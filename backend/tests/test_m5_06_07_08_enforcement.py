"""M5-06/07/08 acceptance — gateway config, EMQX ACL enforcement, rotation.

M5-06: GET /gateways/{id}/config/ returns the R2 contract payload (org/farm
identity, MQTT endpoint, credential SHAPE — bcrypt hash, never the one-time
plaintext) pre- and post-approval; a rejected or suspended gateway gets 403
so its config cannot be refreshed while its traffic is denied.

M5-07: the backend's mqtt_acl_rules rows are the source of truth;
sync_gateway_acls pushes allow-rules into EMQX's built-in-DB authorizer and
revoke_gateway_acls empties them (no_match=deny ⇒ suspended gateway cannot
publish). EMQX failures are logged and FNV-soft: DB state remains the truth.

M5-08: staged rotation — the new hash lands in pending_password_hash while
the OLD credential stays active at the broker (no lockout); the commit
happens ONLY on device ACK proving receipt of the new password; staged
rotations older than 24 h are flagged for operator follow-up.

فارسی: پذیرش M5-06/07/08 — پیکربندی امنِ دروازه، اعمالِ ACL روی بروکر و
چرخشِ اطمینان‌محورِ اعتبارنامه با تأیید دستگاه.
"""

import uuid as _uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import OrgMember  # noqa: E402
from apps.devices.models import GatewaySlot  # noqa: E402
from apps.farms.models import Farm  # noqa: E402
from apps.geography.models import City, Province  # noqa: E402
from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential  # noqa: E402
from apps.mqtt_auth.services import (  # noqa: E402
    STALE_ROTATION_HOURS,
    alert_stale_rotations,
    commit_pending_rotation,
    ensure_gateway_user,
    gateway_config_payload,
    hash_password,
    revoke_gateway_acls,
    stage_rotation,
    sync_gateway_acls,
    verify_password,
)
from apps.provisioning.api import (  # noqa: E402
    gateway_config,
    rotate_credential,
    rotation_ack,
)
from apps.tenants.models import Organization  # noqa: E402


@pytest.fixture
def world(django_user_model):
    tag = _uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    manager = django_user_model.objects.create_user(username=f"mgr-{tag}", password="x")
    OrgMember.objects.create(user=manager, org=org, role="manager", is_active=True)
    worker = django_user_model.objects.create_user(username=f"wkr-{tag}", password="x")
    OrgMember.objects.create(user=worker, org=org, role="worker", is_active=True)
    other_org = Organization.objects.create(name=f"other-{tag}")
    other_manager = django_user_model.objects.create_user(username=f"omgr-{tag}", password="x")
    OrgMember.objects.create(user=other_manager, org=other_org, role="manager", is_active=True)

    def make_slot(status="PENDING_APPROVAL"):
        return GatewaySlot.objects.create(
            slot_id=f"GW-{_uuid.uuid4().hex[:6]}",
            org=org,
            farm=farm,
            created_by=manager,
            provisioning_status=status,
        )

    return {
        "org": org,
        "farm": farm,
        "manager": manager,
        "worker": worker,
        "claims": {"org_id": str(org.org_id), "roles": ["manager"]},
        "worker_claims": {"org_id": str(org.org_id), "roles": ["worker"]},
        "other_manager": other_manager,
        "other_claims": {"org_id": str(other_org.org_id), "roles": ["manager"]},
        "make_slot": make_slot,
    }


def _credential(slot, raw="secret-1"):
    return MqttDeviceCredential.objects.create(
        device_id=slot.slot_id,
        username=slot.slot_id,
        password_hash=hash_password(raw),
    )


def _api(view, user, claims, path, data=None, method="post"):
    """Call an api_view function directly; URL kwargs come from the path
    (DRF does not re-parse the URL when the function is invoked directly)."""
    from rest_framework.test import APIRequestFactory, force_authenticate

    request = getattr(APIRequestFactory(), method.lower())(path, data=data or {}, format="json")
    force_authenticate(request, user=user)
    request.jwt_claims = claims
    parts = path.split("/")
    slot_id = parts[parts.index("gateways") + 1]
    return view(request, slot_id=slot_id)


# ---------------------------------------------------------------------------
# M5-06 — gateway config fetch
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_config_works_pre_and_post_approval(world):
    slot = world["make_slot"]("PENDING_APPROVAL")
    credential = _credential(slot)

    response = _api(gateway_config, world["manager"], world["claims"],
                    f"/api/v1/gateways/{slot.slot_id}/config/", method="get")

    assert response.status_code == 200
    body = response.data
    assert body["gateway_slot_id"] == slot.slot_id
    assert body["org_id"] == str(world["org"].org_id)
    assert body["farm_id"] == world["farm"].pk
    assert body["mqtt"]["host"] and body["mqtt"]["port"]
    assert body["auth"]["username"] == slot.slot_id
    assert body["auth"]["password_hash"] == credential.password_hash
    assert body["auth"]["password_issued_once"] is True
    assert body["auth"]["rotation_pending"] is False
    assert "password" not in body["auth"]  # plaintext is NEVER echoed

    slot.provisioning_status = "PROVISIONED"
    slot.save(update_fields=["provisioning_status"])
    response = _api(gateway_config, world["manager"], world["claims"],
                    f"/api/v1/gateways/{slot.slot_id}/config/", method="get")
    assert response.status_code == 200


@pytest.mark.parametrize("status", ["REJECTED", "SUSPENDED"])
@pytest.mark.django_db
def test_config_403_on_rejected_and_suspended(world, status):
    slot = world["make_slot"](status)
    _credential(slot)
    response = _api(gateway_config, world["manager"], world["claims"],
                    f"/api/v1/gateways/{slot.slot_id}/config/", method="get")
    assert response.status_code == 403


# --- M5-06 Basic-auth path: the gateway fetches its own config ---


def _basic_headers(username, password):
    import base64

    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"HTTP_AUTHORIZATION": f"Basic {token}"}


def _get(view, path, headers=None, claims=None):
    from rest_framework.test import APIRequestFactory

    request = APIRequestFactory().get(path, **headers or {})
    request.jwt_claims = claims
    parts = path.split("/")
    return view(request, slot_id=parts[parts.index("gateways") + 1])


@pytest.mark.django_db
def test_gateway_fetches_own_config_with_basic_auth(world):
    """The device proves itself with its bcrypt credential (zero-touch)."""
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot, raw="gw-secret")

    response = _get(gateway_config, f"/api/v1/gateways/{slot.slot_id}/config/",
                    headers=_basic_headers(slot.slot_id, "gw-secret"))

    assert response.status_code == 200
    assert response.data["gateway_slot_id"] == slot.slot_id
    assert response.data["auth"]["username"] == slot.slot_id
    assert response.data["auth"]["password_hash"] == credential.password_hash


@pytest.mark.django_db
def test_basic_auth_wrong_password_is_401(world):
    slot = world["make_slot"]("PROVISIONED")
    _credential(slot, raw="gw-secret")
    response = _get(gateway_config, f"/api/v1/gateways/{slot.slot_id}/config/",
                    headers=_basic_headers(slot.slot_id, "wrong"))
    assert response.status_code == 401


@pytest.mark.django_db
def test_config_without_any_credentials_is_401(world):
    slot = world["make_slot"]("PROVISIONED")
    response = _get(gateway_config, f"/api/v1/gateways/{slot.slot_id}/config/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_valid_basic_creds_do_not_open_a_locked_gateway(world):
    """REJECTED/SUSPENDED wins even over valid device credentials — the
    status fence is the point of the trust boundary."""
    slot = world["make_slot"]("SUSPENDED")
    _credential(slot, raw="gw-secret")
    response = _get(gateway_config, f"/api/v1/gateways/{slot.slot_id}/config/",
                    headers=_basic_headers(slot.slot_id, "gw-secret"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_device_cannot_fetch_another_gateways_config(world):
    """Valid identity asking about a DIFFERENT gateway is a probe → 404."""
    slot_a = world["make_slot"]("PROVISIONED")
    slot_b = world["make_slot"]("PROVISIONED")
    _credential(slot_a, raw="gw-secret")
    response = _get(gateway_config, f"/api/v1/gateways/{slot_b.slot_id}/config/",
                    headers=_basic_headers(slot_a.slot_id, "gw-secret"))
    assert response.status_code == 404


@pytest.mark.django_db
def test_config_requires_manager_role_and_same_org(world):
    slot = world["make_slot"]("PROVISIONED")
    _credential(slot)
    assert _api(gateway_config, world["worker"], world["worker_claims"],
                f"/api/v1/gateways/{slot.slot_id}/config/", method="get").status_code == 403
    assert _api(gateway_config, world["other_manager"], world["other_claims"],
                f"/api/v1/gateways/{slot.slot_id}/config/", method="get").status_code == 404


# ---------------------------------------------------------------------------
# M5-07 — EMQX ACL sync and revocation
# ---------------------------------------------------------------------------


def _capture_emqx(monkeypatch, responses=None):
    """Replace the EMQX HTTP client with an in-process recorder."""
    calls = []
    responses = responses or []

    def fake(method, path, body=None, timeout=5):
        calls.append((method, path, body))
        if responses:
            return responses.pop(0)
        return 204, None

    monkeypatch.setattr("apps.mqtt_auth.services._emqx_request", fake)
    return calls


@pytest.mark.django_db
def test_sync_pushes_allow_rules_to_emqx(world, monkeypatch):
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot)
    topic = f"{world['org'].org_id}/farms/{slot.farm_id}/gateways/{slot.slot_id}/telemetry"
    MqttAclRule.objects.create(username=credential.username, topic=topic, permission="allow", action="publish")

    calls = _capture_emqx(monkeypatch)
    result = sync_gateway_acls(slot)

    assert result == {"synced": 1, "reachable": True}
    # call 0: authn-user probe; call 1: rules sync
    probe_method, probe_path, _ = calls[0]
    assert probe_method == "GET"
    assert f"/authentication/password_based:built_in_database/users/{slot.slot_id}" in probe_path
    method, path, body = calls[1]
    assert method == "PUT"
    assert f"/rules/users/{slot.slot_id}" in path
    assert body == {
        "username": credential.username,
        "rules": [{"permission": "allow", "action": "publish", "topic": topic}],
    }


@pytest.mark.django_db
def test_revoke_empties_rules_so_no_match_deny_applies(world, monkeypatch):
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot)
    MqttAclRule.objects.create(username=credential.username, topic="org/x/gateway/y/#", permission="allow", action="publish")

    calls = _capture_emqx(monkeypatch)
    assert revoke_gateway_acls(slot) is True

    _, path, body = calls[0]
    assert f"/rules/users/{slot.slot_id}" in path
    assert body == {"username": credential.username, "rules": []}


@pytest.mark.django_db
def test_emqx_failure_is_soft_db_rules_remain(world, monkeypatch):
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot)
    rule = MqttAclRule.objects.create(username=credential.username, topic="org/x/gateway/y/#", permission="allow", action="publish")

    def unreachable(method, path, body=None, timeout=5):
        # what the real client does on an unreachable broker
        return 0, None

    monkeypatch.setattr("apps.mqtt_auth.services._emqx_request", unreachable)
    assert sync_gateway_acls(slot) == {"synced": 0, "reachable": False}
    # DB rules untouched — source of truth survives broker outage
    assert MqttAclRule.objects.filter(pk=rule.pk).exists()


@pytest.mark.django_db
def test_sync_notes_missing_authn_user_but_still_syncs_rules(world, monkeypatch):
    """A missing EMQX authn user (approval-time broker hiccup) is logged, not
    fatal: rules still sync — the warn tells the operator to rotate, whose
    404-heal recreates the user."""
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot)
    MqttAclRule.objects.create(
        username=credential.username,
        topic=f"{world['org'].org_id}/farms/{slot.farm_id}/gateways/{slot.slot_id}/telemetry",
        permission="allow", action="publish",
    )

    calls = _capture_emqx(monkeypatch, responses=[(404, None), (204, None)])
    result = sync_gateway_acls(slot)

    assert result == {"synced": 1, "reachable": True}
    assert calls[0][0] == "GET" and calls[1][0] == "PUT"


@pytest.mark.django_db
def test_ensure_gateway_user_creates_with_plaintext(world, monkeypatch):
    """EMQX 5.5 rejects pre-hashed password_hash, so the plaintext goes to the
    broker at approval; the app DB keeps the $2b$ hash for Basic-auth."""
    calls = _capture_emqx(monkeypatch, responses=[(201, None)])
    assert ensure_gateway_user("GW-USER-1", "s3cret-plaintext") is True
    method, path, body = calls[0]
    assert method == "POST"
    assert "/api/v5/authentication/password_based:built_in_database/users" == path
    assert body == {"user_id": "GW-USER-1", "password": "s3cret-plaintext"}


@pytest.mark.django_db
def test_ensure_gateway_user_is_soft_on_broker_failure(world, monkeypatch):
    def unreachable(method, path, body=None, timeout=5):
        return 0, None

    monkeypatch.setattr("apps.mqtt_auth.services._emqx_request", unreachable)
    # never raises; user creation fails soft — rotation heals later
    assert ensure_gateway_user("GW-USER-2", "pw") is False


# ---------------------------------------------------------------------------
# M5-08 — staged credential rotation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_stage_keeps_old_hash_active_and_sets_pending(world):
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot, raw="old-secret")
    old_hash = credential.password_hash

    stage_rotation(slot.slot_id, "new-secret")

    credential.refresh_from_db()
    assert credential.password_hash == old_hash  # broker keeps working
    assert credential.pending_password_hash != old_hash
    assert credential.pending_password_hash.startswith("$2b$")
    assert credential.pending_since is not None
    assert verify_password("new-secret", credential.pending_password_hash)


@pytest.mark.django_db
def test_commit_happens_only_on_device_ack_with_new_password(world, monkeypatch):
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot, raw="old-secret")
    stage_rotation(slot.slot_id, "new-secret")
    calls = _capture_emqx(monkeypatch, responses=[(200, None)])

    # wrong presented password → no commit AND no EMQX push
    assert commit_pending_rotation(slot.slot_id, "not-the-new-password") is False
    assert calls == []
    credential.refresh_from_db()
    assert credential.password_hash != credential.pending_password_hash

    # correct ACK → staged hash promoted, rotation recorded, EMQX updated
    assert commit_pending_rotation(slot.slot_id, "new-secret") is True
    credential.refresh_from_db()
    assert verify_password("new-secret", credential.password_hash)
    method, path, body = calls[0]
    assert method == "PUT"
    assert f"/api/v5/authentication/password_based:built_in_database/users/{slot.slot_id}" == path
    assert body == {"password": "new-secret"}


@pytest.mark.django_db
def test_commit_heals_missing_emqx_user(world, monkeypatch):
    """Rotation ACK against a broker whose authn user is missing (approval
    hiccup, wiped broker) falls back to user creation — the rotation
    self-converges the broken broker state."""
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot, raw="old-secret")
    stage_rotation(slot.slot_id, "new-secret")
    calls = _capture_emqx(monkeypatch, responses=[(404, None), (201, None)])

    assert commit_pending_rotation(slot.slot_id, "new-secret") is True

    assert calls[0][0] == "PUT"  # update attempt → 404
    assert calls[1][0] == "POST"  # heal: create the user with the ACK plaintext
    assert calls[1][2] == {"user_id": slot.slot_id, "password": "new-secret"}
    credential.refresh_from_db()  # the service saved through its OWN instance
    assert credential.pending_password_hash is None
    assert credential.pending_since is None
    assert credential.last_rotated_at is not None

    # nothing staged anymore → ack rejected
    assert commit_pending_rotation(slot.slot_id, "new-secret") is False


@pytest.mark.django_db
def test_rotation_endpoint_issues_plaintext_once_then_commits_on_ack(world):
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot, raw="old-secret")

    response = _api(rotate_credential, world["manager"], world["claims"],
                    f"/api/v1/gateways/{slot.slot_id}/rotate-credential/")
    assert response.status_code == 200
    new_plain = response.data["password"]
    assert response.data["rotation_pending"] is True
    assert new_plain  # one-time issue

    credential.refresh_from_db()
    assert verify_password("old-secret", credential.password_hash)  # old still live
    assert credential.pending_password_hash.startswith("$2b$")

    # the config endpoint must NOT reveal the plaintext a second time
    config = _api(gateway_config, world["manager"], world["claims"],
                  f"/api/v1/gateways/{slot.slot_id}/config/", method="get")
    assert config.data["auth"]["rotation_pending"] is True
    assert "password" not in config.data["auth"]

    # ACK with the issued password commits the rotation
    ack = _api(rotation_ack, world["manager"], world["claims"],
               f"/api/v1/gateways/{slot.slot_id}/rotation-ack/",
               data={"new_password": new_plain})
    assert ack.status_code == 200
    assert ack.data["rotation_committed"] is True
    credential.refresh_from_db()
    assert verify_password(new_plain, credential.password_hash)
    assert credential.pending_password_hash is None


@pytest.mark.django_db
def test_rotation_requires_provisioned_gateway(world):
    slot = world["make_slot"]("PENDING_APPROVAL")
    response = _api(rotate_credential, world["manager"], world["claims"],
                    f"/api/v1/gateways/{slot.slot_id}/rotate-credential/")
    assert response.status_code == 409


@pytest.mark.django_db
def test_stale_rotation_alert_flags_unacked_after_24h(world):
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot, raw="old-secret")
    fresh = stage_rotation(slot.slot_id, "new-secret")
    assert alert_stale_rotations(now_dt=timezone.now()) == 0  # just staged

    stale_ts = timezone.now() + timedelta(hours=STALE_ROTATION_HOURS + 1)
    assert alert_stale_rotations(now_dt=stale_ts) == 1

    # after ack, the same sweep finds nothing
    commit_pending_rotation(slot.slot_id, "new-secret")
    assert alert_stale_rotations(now_dt=stale_ts + timedelta(hours=1)) == 0


@pytest.mark.django_db
def test_gateway_config_payload_contract(world):
    """R2 payload shape pinned: org/farm ids, NO sensor slot_ids."""
    slot = world["make_slot"]("PROVISIONED")
    credential = _credential(slot)
    payload = gateway_config_payload(slot, credential)
    assert payload["org_id"] == str(world["org"].org_id)
    assert payload["farm_id"] == world["farm"].pk
    assert payload["provisioning_status"] == "PROVISIONED"
    assert set(payload) == {"gateway_slot_id", "org_id", "farm_id", "provisioning_status", "mqtt", "auth", "rate_limit_note"}
    assert "slot_ids" not in str(payload)
    assert "sensors" not in str(payload)