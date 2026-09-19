import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import OrgMember
from apps.devices.models import (
    GatewayHardwareRegistry,
    GatewayRequest,
    GatewaySaloonZone,
    GatewaySensorMapping,
    GatewaySlot,
    SaloonPosition,
    SaloonSensorSlot,
)
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
from apps.provisioning.api import gateway_requests, provisioning_sync, review_gateway_request
from apps.provisioning.services import SYNC_LOCK_KEY
from apps.system_config.models import SystemConfiguration
from apps.tenants.models import Organization


@pytest.fixture
def provisioning_world(django_user_model):
    province = Province.objects.create(name="P5")
    city = City.objects.create(province=province, name="C5")
    org = Organization.objects.create(name="provisioning-org")
    farm = Farm.objects.create(org=org, name="farm", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="saloon")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    sensor = SaloonSensorSlot.objects.create(
        slot_id="SENSOR-M5", saloon=saloon, org=org, position=position
    )
    user = django_user_model.objects.create_user(username="manager-m5", password="x")
    OrgMember.objects.create(user=user, org=org, role="manager", is_active=True)
    installer = django_user_model.objects.create_user(username="installer-m5", password="x")
    OrgMember.objects.create(user=installer, org=org, role="installer", is_active=True)
    manager = user
    return locals()


def _request(view, method, path, user, claims, data=None, **kwargs):
    request = getattr(APIRequestFactory(), method.lower())(path, data=data or {}, format="json")
    force_authenticate(request, user=user)
    request.jwt_claims = claims
    return view(request, **kwargs)


@pytest.mark.django_db
def test_manager_can_submit_gateway_request(provisioning_world):
    w = provisioning_world
    response = _request(
        gateway_requests, "POST", "/api/v1/gateway-requests/", w["user"],
        {"org_id": str(w["org"].org_id), "roles": ["manager"]},
        {"farm_id": w["farm"].pk, "details": {"model": "edge"}},
    )
    assert response.status_code == 201
    assert GatewayRequest.objects.get().requested_by_id == w["user"].pk


@pytest.mark.django_db
def test_worker_cannot_submit_gateway_request(provisioning_world):
    w = provisioning_world
    response = _request(
        gateway_requests, "POST", "/api/v1/gateway-requests/", w["installer"],
        {"org_id": str(w["org"].org_id), "roles": ["worker"]}, {},
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_installer_sync_derives_hardware_and_enters_pending_approval(provisioning_world, monkeypatch, fake_redis):
    w = provisioning_world
    slot = GatewaySlot.objects.create(slot_id="GW-M5-SYNC", org=w["org"], farm=w["farm"], created_by=w["manager"])
    hardware = GatewayHardwareRegistry.objects.create(
        hardware_id="HW-SERVER-DERIVED", slot=slot, org=w["org"], installed_by=w["installer"]
    )
    monkeypatch.setattr("apps.provisioning.services._redis_client", fake_redis)
    response = _request(
        provisioning_sync, "POST", "/api/v1/provisioning/sync/", w["installer"],
        {"org_id": str(w["org"].org_id), "roles": ["installer"]},
        {
            "gateway_slot_id": slot.slot_id,
            "hardware_id": "FORGED-HARDWARE",
            "zones": [w["saloon"].pk],
            "sensor_slot_mappings": [{"pack_id": "P-A", "slot_id": w["sensor"].slot_id}],
        },
    )
    assert response.status_code == 200
    slot.refresh_from_db()
    assert slot.provisioning_status == "PENDING_APPROVAL"
    assert GatewayHardwareRegistry.objects.get(pk=hardware.pk).hardware_id == "HW-SERVER-DERIVED"
    mapping = GatewaySensorMapping.objects.get()
    assert mapping.gateway_hardware_id == "HW-SERVER-DERIVED"
    assert GatewaySaloonZone.objects.filter(gateway_slot=slot, saloon=w["saloon"]).exists()
    assert cache.get(f"gateway:status:{slot.slot_id}") is None


@pytest.mark.django_db
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_installer_sync_applies_hardware_replacement(provisioning_world, monkeypatch, fake_redis):
    """M5-03 replacements: the OLD unit's registry row retires, the pack
    re-points under the NEW unit, and the stale old-unit mapping is gone."""
    w = provisioning_world
    slot = GatewaySlot.objects.create(slot_id="GW-M5-REPL", org=w["org"], farm=w["farm"], created_by=w["manager"])
    old_hw = GatewayHardwareRegistry.objects.create(hardware_id="HW-OLD", slot=slot, org=w["org"], installed_by=w["installer"])
    new_hw = GatewayHardwareRegistry.objects.create(hardware_id="HW-NEW", slot=slot, org=w["org"], installed_by=w["installer"])
    GatewaySensorMapping.objects.create(
        gateway_hardware_id="HW-OLD", pack_id="P-A", slot=w["sensor"], org=w["org"]
    )
    monkeypatch.setattr("apps.provisioning.services._redis_client", fake_redis)

    response = _request(
        provisioning_sync, "POST", "/api/v1/provisioning/sync/", w["installer"],
        {"org_id": str(w["org"].org_id), "roles": ["installer"]},
        {
            "gateway_slot_id": slot.slot_id,
            "hardware_id": "FORGED-IGNORED",
            "zones": [],
            "sensor_slot_mappings": [],
            "replacements": [{
                "old_hardware_id": "HW-OLD",
                "new_hardware_id": "HW-NEW",
                "pack_id": "P-A",
                "slot_id": w["sensor"].slot_id,
            }],
        },
    )
    assert response.status_code == 200
    assert response.data["replacements_count"] == 1
    assert response.data["gateway_hardware_id"] == "HW-NEW"

    old_hw.refresh_from_db()
    assert old_hw.removed_at is not None  # retired server-side
    new_hw.refresh_from_db()
    assert new_hw.removed_at is None
    mapping = GatewaySensorMapping.objects.get(gateway_hardware_id="HW-NEW", pack_id="P-A")
    assert mapping.slot_id == w["sensor"].slot_id
    assert not GatewaySensorMapping.objects.filter(gateway_hardware_id="HW-OLD", pack_id="P-A").exists()
    assert GatewayHardwareRegistry.objects.get(pk=old_hw.pk).removed_by_id == w["installer"].pk


@pytest.mark.django_db
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_replacement_with_unknown_hardware_is_400_and_writes_nothing(provisioning_world, monkeypatch, fake_redis):
    """A malformed replacement must never produce partial topology (M5-09)."""
    w = provisioning_world
    slot = GatewaySlot.objects.create(slot_id="GW-M5-REPL-BAD", org=w["org"], farm=w["farm"], created_by=w["manager"])
    old_hw = GatewayHardwareRegistry.objects.create(hardware_id="HW-ONLY", slot=slot, org=w["org"], installed_by=w["installer"])
    monkeypatch.setattr("apps.provisioning.services._redis_client", fake_redis)

    response = _request(
        provisioning_sync, "POST", "/api/v1/provisioning/sync/", w["installer"],
        {"org_id": str(w["org"].org_id), "roles": ["installer"]},
        {
            "gateway_slot_id": slot.slot_id,
            "zones": [w["saloon"].pk],
            "sensor_slot_mappings": [{"pack_id": "P-A", "slot_id": w["sensor"].slot_id}],
            "replacements": [{
                "old_hardware_id": "HW-ONLY",
                "new_hardware_id": "HW-GHOST",
                "pack_id": "P-A",
                "slot_id": w["sensor"].slot_id,
            }],
        },
    )
    assert response.status_code == 400
    # zero partial writes: nothing retired, no zones, no mappings, status
    # unchanged — replacement validation runs before ANY write
    old_hw.refresh_from_db()
    assert old_hw.removed_at is None
    assert GatewaySaloonZone.objects.filter(gateway_slot=slot).count() == 0
    assert GatewaySensorMapping.objects.count() == 0
    slot.refresh_from_db()
    assert slot.provisioning_status == "PENDING_INSTALLATION"


@pytest.mark.django_db
def test_sync_conflict_returns_retry_after(provisioning_world, monkeypatch, fake_redis):
    w = provisioning_world
    slot = GatewaySlot.objects.create(slot_id="GW-M5-CONFLICT", org=w["org"], farm=w["farm"], created_by=w["installer"])
    GatewayHardwareRegistry.objects.create(hardware_id="HW-CONFLICT", slot=slot, org=w["org"])
    client = fake_redis
    monkeypatch.setattr("apps.provisioning.services._redis_client", client)
    client.set(SYNC_LOCK_KEY.format(slot_id=slot.slot_id), "someone-else", ex=30)
    response = _request(
        provisioning_sync, "POST", "/api/v1/provisioning/sync/", w["installer"],
        {"org_id": str(w["org"].org_id), "roles": ["installer"]}, {"gateway_slot_id": slot.slot_id},
    )
    assert response.status_code == 409
    assert response["Retry-After"] == "3"
    assert GatewaySensorMapping.objects.count() == 0
    client.delete(SYNC_LOCK_KEY.format(slot_id=slot.slot_id))


# ---------------------------------------------------------------------------
# M5-02 review endpoint — platform reviewers (admin/support default)
# ---------------------------------------------------------------------------


def _submit_request(w, user=None):
    """Helper: a manager submits a gateway request; returns the request id."""
    user = user or w["user"]
    response = _request(
        gateway_requests, "POST", "/api/v1/gateway-requests/", user,
        {"org_id": str(w["org"].org_id), "roles": ["manager"]},
        {"farm_id": w["farm"].pk, "details": {"model": "edge"}},
    )
    assert response.status_code == 201
    return response.data["id"]


@pytest.mark.parametrize("role", ["admin", "support"])
@pytest.mark.django_db
def test_review_approval_creates_slot_credentials_and_acl(provisioning_world, django_user_model, role, monkeypatch):
    """Acceptance 'approval creates slot+credentials': the platform reviewer
    (admin OR support by default) approves a pending request; the slot, the
    bcrypt credential and the EMQX allow-rules appear; the plaintext password
    is returned exactly once and pushed to the broker (mocked here — the
    live-broker dance is the WALKTHROUGH)."""
    w = provisioning_world
    emqx_calls = []

    import apps.mqtt_auth.services as svc

    def fake_emqx(method, path, body=None, timeout=5):
        # Chain probe (2026-09-19 JWT shape): answered present, not recorded.
        if method == "GET" and path == "/api/v5/authentication":
            return 200, [{"id": svc.AUTHN_CHAIN_ID}]
        emqx_calls.append((method, path, body))
        return 201, None

    monkeypatch.setattr(svc, "_emqx_request", fake_emqx)
    monkeypatch.setattr(svc, "_emqx_login", lambda: "test-jwt")
    svc._builtin_chain["known"] = None
    svc._emqx_token["value"] = None
    reviewer = django_user_model.objects.create_user(username=f"reviewer-{role}", password="x")
    reviewer.is_platform_staff = True
    reviewer.staff_role = role
    reviewer.save()
    request_id = _submit_request(w)

    response = _request(
        review_gateway_request, "POST",
        f"/api/v1/gateway-requests/{request_id}/review/", reviewer, {},
        {"decision": "APPROVE", "slot_id": "GW-REVIEW-1"},
        request_id=request_id,
    )

    assert response.status_code == 200
    assert response.data["status"] == "APPROVED"
    assert response.data["gateway_slot_id"] == "GW-REVIEW-1"
    assert response.data["username"] == "GW-REVIEW-1"
    assert response.data["password"]  # one-time plaintext issue

    slot = GatewaySlot.objects.get(slot_id="GW-REVIEW-1")
    assert slot.provisioning_status == "PENDING_INSTALLATION"
    credential = MqttDeviceCredential.objects.get(username=slot.slot_id)
    assert credential.password_hash.startswith("$2b$")  # EMQX-native (C7)
    # Allow rules MUST name the topics the gateway actually uses (org/farm/
    # gateway segments — the subscriber grammar; a rule on a topic the gateway
    # never uses would be dead weight under no_match=deny). M8-04-H (C5):
    # the full firmware contract — 5 publishes (telemetry T1, status T2,
    # commands/response T4, ota/response T7, diagnostics/response T9) + 4
    # subscriptions (commands T3, clock/sync T5, ota T6, diagnostics/request T8).
    from apps.provisioning.api import gateway_acl_rule_items

    expected = {
        (r["topic"], r["action"])
        for r in gateway_acl_rule_items(str(w["org"].org_id), w["farm"].pk, slot.slot_id)
    }
    rules = list(MqttAclRule.objects.filter(username=credential.username))
    assert len(rules) == len(expected) == 9
    assert all(r.permission == "allow" for r in rules)
    assert {(r.topic, r.action) for r in rules} == expected
    # The EMQX authn user was created with the one-time plaintext (broker-side
    # authn — EMQX 5.5 rejects pre-hashed password fields).
    assert any(
        method == "POST"
        and body == {"user_id": slot.slot_id, "password": response.data["password"]}
        for method, _path, body in emqx_calls
    )
    request_row = GatewayRequest.objects.get(pk=request_id)
    assert request_row.approved_by_id == reviewer.pk


@pytest.mark.django_db
def test_review_reject_records_reason_and_no_slot(provisioning_world, django_user_model):
    w = provisioning_world
    reviewer = django_user_model.objects.create_user(username="reviewer-reject", password="x")
    reviewer.is_platform_staff = True
    reviewer.staff_role = "admin"
    reviewer.save()
    request_id = _submit_request(w)

    response = _request(
        review_gateway_request, "POST",
        f"/api/v1/gateway-requests/{request_id}/review/", reviewer, {},
        {"decision": "REJECT", "reason": "farm does not exist"},
        request_id=request_id,
    )

    assert response.status_code == 200
    assert response.data["status"] == "REJECTED"
    assert response.data["rejection_reason"] == "farm does not exist"
    assert GatewaySlot.objects.count() == 0
    assert MqttDeviceCredential.objects.count() == 0


@pytest.mark.django_db
def test_manager_cannot_review_and_double_review_is_409(provisioning_world, django_user_model):
    w = provisioning_world
    reviewer = django_user_model.objects.create_user(username="reviewer-409", password="x")
    reviewer.is_platform_staff = True
    reviewer.staff_role = "admin"
    reviewer.save()
    request_id = _submit_request(w)

    forbidden = _request(
        review_gateway_request, "POST",
        f"/api/v1/gateway-requests/{request_id}/review/", w["user"],
        {"org_id": str(w["org"].org_id), "roles": ["manager"]},
        {"decision": "APPROVE"}, request_id=request_id,
    )
    assert forbidden.status_code == 403

    first = _request(
        review_gateway_request, "POST",
        f"/api/v1/gateway-requests/{request_id}/review/", reviewer, {},
        {"decision": "APPROVE"}, request_id=request_id,
    )
    assert first.status_code == 200
    again = _request(
        review_gateway_request, "POST",
        f"/api/v1/gateway-requests/{request_id}/review/", reviewer, {},
        {"decision": "APPROVE"}, request_id=request_id,
    )
    assert again.status_code == 409  # already reviewed


@pytest.mark.django_db
def test_pending_request_cap_400s_at_limit(provisioning_world):
    """Acceptance 'cap 400s at limit' — the cap is config-driven (D21)."""
    w = provisioning_world
    SystemConfiguration.objects.filter(pk="max_pending_gateway_requests").update(value="2")
    for _ in range(2):
        assert _submit_request(w) >= 0

    response = _request(
        gateway_requests, "POST", "/api/v1/gateway-requests/", w["user"],
        {"org_id": str(w["org"].org_id), "roles": ["manager"]},
        {"farm_id": w["farm"].pk},
    )
    assert response.status_code == 400
    assert "cap" in response.data["error"]
