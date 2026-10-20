"""Phase 1 acceptance — factory inventory + the pre-org broker lane.

Every assertion maps to a Phase 1 task in the migration plan. The one assertion
that matters most is `test_unbound_unit_acl_reaches_no_tenant_topic`: the whole
security argument for AD-1 is that a factory-registered device is strictly LESS
privileged than a provisioned gateway, and that only holds if its ACL set is
exactly three literal rows.
"""

import re

import pytest
from django.db import connection
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.devices.api_units import unit_claim_code, unit_collection, unit_credential, unit_retire
from apps.auditing.models import PlatformAuditLog
from apps.devices.models import DeviceUnit
from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
from apps.mqtt_auth.services import bootstrap_acl_rule_items, verify_password

pytestmark = pytest.mark.django_db

UNITS_PATH = "/api/v1/platform/devices/units/"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _staff(django_user_model, username, role="hardware"):
    user = django_user_model.objects.create_user(username=username, password="x")
    user.is_platform_staff = True
    user.staff_role = role
    user.save(update_fields=["is_platform_staff", "staff_role"])
    return user


def _plain(django_user_model, username="plain-user"):
    return django_user_model.objects.create_user(username=username, password="x")


def _call(view, method, path, user, data=None, **kwargs):
    request = getattr(APIRequestFactory(), method.lower())(path, data=data or {}, format="json")
    force_authenticate(request, user=user)
    return view(request, **kwargs)


def _register(django_user_model, user, **overrides):
    payload = {"hardware_id": "AA:BB:CC:DD:EE:01", "batch": "batch-7"}
    payload.update(overrides)
    return _call(unit_collection, "POST", UNITS_PATH, user, payload)


# ---------------------------------------------------------------------------
# the gate: platform staff, org-less, and NOT the support-op surface
# ---------------------------------------------------------------------------

def test_non_staff_cannot_register(django_user_model):
    response = _register(django_user_model, _plain(django_user_model))
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["support", "engineer", "installer", "hardware"])
def test_write_roles_are_checked(django_user_model, role):
    """Only `hardware` and `admin` may write. Support reads but never writes."""
    response = _register(django_user_model, _staff(django_user_model, f"staff-{role}", role))
    assert response.status_code == (201 if role in {"hardware", "admin"} else 403)


def test_superuser_can_register(django_user_model):
    user = django_user_model.objects.create_superuser(username="root", password="x", email="r@x")
    assert _register(django_user_model, user).status_code == 201


# ---------------------------------------------------------------------------
# what one call mints
# ---------------------------------------------------------------------------

def test_register_mints_unit_credential_and_exactly_three_acls(django_user_model):
    user = _staff(django_user_model, "hw-1")
    response = _register(django_user_model, user)

    assert response.status_code == 201
    body = response.data
    unit = DeviceUnit.objects.get(pk=body["unit_id"])

    assert unit.hardware_id == "AA:BB:CC:DD:EE:01"
    assert unit.state == "REGISTERED"
    assert unit.org is None if hasattr(unit, "org") else True
    assert unit.created_by == user

    credential = MqttDeviceCredential.objects.get(device_id=body["unit_id"])
    assert credential.username == body["unit_id"]
    assert credential.is_active is True
    assert verify_password(body["password"], credential.password_hash)

    rules = MqttAclRule.objects.filter(username=body["unit_id"])
    assert rules.count() == 4  # + command/response (D4) since 2026-10-20
    assert body["acl_rules"] == 4

    # The claim code is stored as a hash, never readable again.
    assert verify_password(body["claim_code"], unit.claim_code_hash)


def test_generated_unit_id_is_permanent_and_shaped(django_user_model):
    user = _staff(django_user_model, "hw-2")
    response = _register(django_user_model, user)
    assert re.fullmatch(r"GW-[0-9A-F]{16}", response.data["unit_id"])


def test_supplied_unit_id_becomes_the_slot_id(django_user_model):
    user = _staff(django_user_model, "hw-3")
    response = _register(django_user_model, user, unit_id="GW-2026-0042")
    assert response.data["unit_id"] == "GW-2026-0042"
    assert DeviceUnit.objects.get(pk="GW-2026-0042").state == "REGISTERED"


def test_duplicate_hardware_id_conflicts(django_user_model):
    user = _staff(django_user_model, "hw-4")
    _register(django_user_model, user, unit_id="GW-A", hardware_id="AA:BB:CC:DD:EE:44")
    response = _register(django_user_model, user, unit_id="GW-B", hardware_id="AA:BB:CC:DD:EE:44")
    assert response.status_code == 409


def test_duplicate_unit_id_conflicts(django_user_model):
    user = _staff(django_user_model, "hw-5")
    _register(django_user_model, user, unit_id="GW-DUP")
    response = _register(django_user_model, user, unit_id="GW-DUP")
    assert response.status_code == 409


def test_register_writes_a_platform_audit_row_without_secrets(django_user_model):
    user = _staff(django_user_model, "hw-6")
    body = _register(django_user_model, user).data

    row = PlatformAuditLog.objects.get(target_id=body["unit_id"])
    assert row.action == "PLATFORM_UNIT_REGISTER"
    assert row.actor == user
    assert row.details["acl_rules"] == 4
    assert body["password"] not in str(row.details)
    assert body["claim_code"] not in str(row.details)


# ---------------------------------------------------------------------------
# the security core
# ---------------------------------------------------------------------------

def test_unbound_unit_acl_reaches_no_tenant_topic(django_user_model):
    """AD-1: four literal rows, no wildcard, no tenant prefix.

    The fourth is `command/response` (2026-10-20): without it a pre-bind unit's
    ACK/SUCCESS replies are denied and Phase 3's command channel cannot work
    until the device has been bound to a farm.
    """
    user = _staff(django_user_model, "hw-7")
    body = _register(django_user_model, user).data
    unit_id = body["unit_id"]

    topics = set(MqttAclRule.objects.filter(username=unit_id).values_list("topic", flat=True))
    assert topics == {
        f"devices/{unit_id}/desired",
        f"devices/{unit_id}/command",
        f"devices/{unit_id}/reported",
        f"devices/{unit_id}/command/response",
    }
    for topic in topics:
        assert "+" not in topic and "#" not in topic
        assert "/farms/" not in topic


def test_bootstrap_acl_helper_emits_exactly_four_rows():
    # FOUR rows since 2026-10-20: the device's own command-response lane was
    # missing, so a pre-bind unit's ACK/SUCCESS replies were denied by
    # no_match=deny and Phase 3's command channel could not work at all before
    # binding. The lane needs a permission on BOTH sides; this is the device's.
    items = bootstrap_acl_rule_items("GW-X")
    assert len(items) == 4
    actions = {(i["action"], i["topic"]) for i in items}
    assert actions == {
        ("subscribe", "devices/GW-X/desired"),
        ("subscribe", "devices/GW-X/command"),
        ("publish", "devices/GW-X/reported"),
        ("publish", "devices/GW-X/command/response"),
    }


def test_inventory_never_returns_secrets(django_user_model):
    user = _staff(django_user_model, "hw-8")
    _register(django_user_model, user)
    response = _call(unit_collection, "GET", UNITS_PATH, user)
    assert response.status_code == 200
    blob = str(response.data)
    assert "password" not in blob.lower()
    assert "claim_code" not in blob.lower()
    assert "claim_code_hash" not in blob


def test_support_can_read_the_inventory(django_user_model):
    user = _staff(django_user_model, "sup-1", "support")
    _register(django_user_model, _staff(django_user_model, "hw-9"))
    response = _call(unit_collection, "GET", UNITS_PATH, user)
    assert response.status_code == 200
    assert response.data["count"] == 1


def test_plain_user_cannot_read_the_inventory(django_user_model):
    _register(django_user_model, _staff(django_user_model, "hw-10"))
    response = _call(unit_collection, "GET", UNITS_PATH, _plain(django_user_model, "plain-2"))
    assert response.status_code == 403


def test_list_state_filter(django_user_model):
    user = _staff(django_user_model, "hw-11")
    _register(django_user_model, user, unit_id="GW-REG", hardware_id="HW-REG")
    _register(django_user_model, user, unit_id="GW-RET", hardware_id="HW-RET")
    _call(unit_retire, "POST", UNITS_PATH + "GW-RET/retire/", user, {"reason": "dead unit"}, unit_id="GW-RET")

    assert _call(unit_collection, "GET", UNITS_PATH + "?state=REGISTERED", user).data["count"] == 1
    assert _call(unit_collection, "GET", UNITS_PATH + "?state=RETIRED", user).data["count"] == 1
    bad = _call(unit_collection, "GET", UNITS_PATH + "?state=NOPE", user)
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# re-issue, retire
# ---------------------------------------------------------------------------

def test_claim_code_reissue_rotates_the_secret(django_user_model):
    user = _staff(django_user_model, "hw-12")
    unit_id = _register(django_user_model, user, unit_id="GW-CC").data["unit_id"]
    original = DeviceUnit.objects.get(pk=unit_id).claim_code_hash

    response = _call(unit_claim_code, "POST", UNITS_PATH + f"{unit_id}/claim-code/", user, unit_id=unit_id)
    assert response.status_code == 200
    unit = DeviceUnit.objects.get(pk=unit_id)
    assert unit.claim_code_hash != original
    assert verify_password(response.data["claim_code"], unit.claim_code_hash)
    assert PlatformAuditLog.objects.filter(action="PLATFORM_UNIT_CLAIM_CODE").count() == 1


def test_credential_reissue_writes_a_fresh_hash(django_user_model):
    user = _staff(django_user_model, "hw-13")
    unit_id = _register(django_user_model, user, unit_id="GW-CRED").data["unit_id"]

    response = _call(unit_credential, "POST", UNITS_PATH + f"{unit_id}/credential/", user, unit_id=unit_id)
    assert response.status_code == 200
    credential = MqttDeviceCredential.objects.get(device_id=unit_id)
    assert verify_password(response.data["password"], credential.password_hash)
    assert credential.is_active is True


def test_retire_deactivates_credential_and_deletes_acls(django_user_model):
    user = _staff(django_user_model, "hw-14")
    unit_id = _register(django_user_model, user, unit_id="GW-DEAD").data["unit_id"]

    response = _call(unit_retire, "POST", UNITS_PATH + f"{unit_id}/retire/", user, {"reason": "rma"}, unit_id=unit_id)
    assert response.status_code == 200

    unit = DeviceUnit.objects.get(pk=unit_id)
    assert unit.state == "RETIRED"
    assert unit.retired_at is not None
    assert MqttDeviceCredential.objects.get(device_id=unit_id).is_active is False
    assert MqttAclRule.objects.filter(username=unit_id).count() == 0


def test_retire_is_idempotent(django_user_model):
    user = _staff(django_user_model, "hw-15")
    unit_id = _register(django_user_model, user, unit_id="GW-IDEM").data["unit_id"]
    _call(unit_retire, "POST", UNITS_PATH + f"{unit_id}/retire/", user, {"reason": "rma"}, unit_id=unit_id)
    again = _call(unit_retire, "POST", UNITS_PATH + f"{unit_id}/retire/", user, {"reason": "rma"}, unit_id=unit_id)
    assert again.status_code == 200
    assert again.data["idempotent"] is True


def test_retire_requires_a_reason(django_user_model):
    user = _staff(django_user_model, "hw-16")
    unit_id = _register(django_user_model, user, unit_id="GW-NOREASON").data["unit_id"]
    response = _call(
        unit_retire, "POST", UNITS_PATH + f"{unit_id}/retire/", user, {}, unit_id=unit_id
    )
    assert response.status_code == 400


def test_retired_unit_is_404_not_a_resurrection(django_user_model):
    user = _staff(django_user_model, "hw-17")
    unit_id = _register(django_user_model, user, unit_id="GW-GONE").data["unit_id"]
    _call(unit_retire, "POST", UNITS_PATH + f"{unit_id}/retire/", user, {"reason": "rma"}, unit_id=unit_id)
    assert _call(unit_credential, "POST", UNITS_PATH + f"{unit_id}/credential/", user, unit_id=unit_id).status_code == 404
    assert _call(unit_claim_code, "POST", UNITS_PATH + f"{unit_id}/claim-code/", user, unit_id=unit_id).status_code == 404


def test_unknown_unit_and_retired_unit_share_one_verdict(django_user_model):
    """No inventory oracle: a guessed id gets the same answer either way."""
    user = _staff(django_user_model, "hw-18")
    unknown = _call(unit_credential, "POST", UNITS_PATH + "GW-NOPE/credential/", user, unit_id="GW-NOPE")
    unit_id = _register(django_user_model, user, unit_id="GW-RETIRED-2").data["unit_id"]
    _call(unit_retire, "POST", UNITS_PATH + f"{unit_id}/retire/", user, {"reason": "rma"}, unit_id=unit_id)
    retired = _call(unit_credential, "POST", UNITS_PATH + f"{unit_id}/credential/", user, unit_id=unit_id)
    assert unknown.status_code == retired.status_code == 404
    assert unknown.data == retired.data


def test_bound_unit_cannot_be_retired_or_reissued(django_user_model):
    """A gateway referencing the unit would be orphaned."""
    user = _staff(django_user_model, "hw-19")
    unit_id = _register(django_user_model, user, unit_id="GW-BOUND").data["unit_id"]
    DeviceUnit.objects.filter(pk=unit_id).update(state="BOUND")

    retire = _call(unit_retire, "POST", UNITS_PATH + f"{unit_id}/retire/", user, {"reason": "rma"}, unit_id=unit_id)
    claim = _call(unit_claim_code, "POST", UNITS_PATH + f"{unit_id}/claim-code/", user, unit_id=unit_id)
    assert retire.status_code == 409
    assert claim.status_code == 409


# ---------------------------------------------------------------------------
# the authn query — the broker-side half of AD-1
# ---------------------------------------------------------------------------

def test_authn_query_text_allows_bound_and_unbound_units():
    """Guards the emqx.conf edit from a silent regression in wording."""
    from pathlib import Path

    conf = Path(__file__).resolve().parents[2] / "emqx" / "emqx.conf"
    match = re.search(r"query = \"(.*?)\"\n", conf.read_text(encoding="utf-8"), re.S)
    assert match, "authn query not found in emqx.conf"
    sql = match.group(1)

    assert "LEFT JOIN gateway_slots" in sql
    assert "LEFT JOIN device_units" in sql
    assert "du.state IN ('REGISTERED','BOUND')" in sql
    assert "${username}" in sql
    # The slot-status names must survive verbatim.
    assert "gs.provisioning_status IN ('PENDING_INSTALLATION','PENDING_APPROVAL','PROVISIONED')" in sql


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_authn_query_accepts_registered_unit_against_real_sql(django_user_model):
    """Run the query shape over the real schema (RLS-grant tests are Postgres-only)."""
    if connection.vendor != "postgresql":
        pytest.skip("the authn query is Postgres SQL")

    user = _staff(django_user_model, "hw-20")
    unit_id = _register(django_user_model, user, unit_id="GW-SQL").data["unit_id"]
    from pathlib import Path

    conf = Path(__file__).resolve().parents[2] / "emqx" / "emqx.conf"
    sql_template = re.search(r"query = \"(.*?)\"\n", conf.read_text(encoding="utf-8"), re.S).group(1)
    sql = sql_template.replace("${username}", "%s")

    with connection.cursor() as cursor:
        cursor.execute(sql, [unit_id])
        assert cursor.fetchone() is not None

    # A retired unit must stop resolving.
    _call(unit_retire, "POST", UNITS_PATH + f"{unit_id}/retire/", user, {"reason": "rma"}, unit_id=unit_id)
    with connection.cursor() as cursor:
        cursor.execute(sql, [unit_id])
        assert cursor.fetchone() is None
