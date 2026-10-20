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
from apps.provisioning.models import InstallationJob
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


def _submit_request(w, user=None, payload=None):
    """Helper: a manager submits a gateway request; returns the request id.

    `payload` defaults to the pre-AD-15 shape (a reference to an existing farm)
    so tests that are not about farm creation keep passing unchanged.
    """
    user = user or w["user"]
    body = (
        payload
        if payload is not None
        else {"farm_id": w["farm"].pk, "details": {"model": "edge"}}
    )
    response = _request(
        gateway_requests, "POST", "/api/v1/gateway-requests/", user,
        {"org_id": str(w["org"].org_id), "roles": ["manager"]},
        body,
    )
    assert response.status_code == 201
    return response.data["id"]


@pytest.mark.parametrize("role", ["admin", "support"])
@pytest.mark.django_db
def test_review_approval_creates_jobs_and_no_slot(
    provisioning_world, django_user_model, role, monkeypatch
):
    """AD-15/16/17 (2026-10-18): approval creates INSTALLATION JOBS, never a slot.

    The device already exists at the factory (Phase 1) and is joined to a slot by
    the installer scanning its QR claim code (Phase 2 slice c), so minting a
    `gateway_slots` row — with a credential no physical device holds — used to
    leave an orphan here. Approval now: materialises the farm if the request was
    creating one, creates one job per expected gateway, flips the status, audits.

    The response therefore carries NO password: there is nothing here to leak.
    """
    w = provisioning_world
    monkeypatch.setattr(
        "apps.mqtt_auth.services.ensure_gateway_user", lambda *a, **k: True
    )
    reviewer = django_user_model.objects.create_user(username=f"reviewer-{role}", password="x")
    reviewer.is_platform_staff = True
    reviewer.staff_role = role
    reviewer.save()
    request_id = _submit_request(w, payload={"farm_id": w["farm"].pk})

    response = _request(
        review_gateway_request, "POST",
        f"/api/v1/gateway-requests/{request_id}/review/", reviewer, {},
        {"decision": "APPROVE"},
        request_id=request_id,
    )

    assert response.status_code == 200
    assert response.data["status"] == "APPROVED"
    assert response.data["farm_id"] == w["farm"].pk
    assert response.data["jobs_created"] == 1
    # the whole point: no credential surface at approval time
    assert "password" not in response.data
    assert "username" not in response.data
    assert "gateway_slot_id" not in response.data

    assert not GatewaySlot.objects.filter(request_id=request_id).exists()
    assert not MqttDeviceCredential.objects.filter(username__startswith="GW-REVIEW").exists()
    assert not MqttAclRule.objects.filter(username__startswith="GW-REVIEW").exists()

    from apps.provisioning.models import InstallationJob

    job = InstallationJob.objects.get(request_id=request_id)
    assert job.kind == "INSTALL"
    assert job.state == "PENDING"
    assert job.assigned_installer_id is None
    assert job.farm_id == w["farm"].pk
    assert job.gateway_slot_id is None
    assert job.unit_id is None

    row = GatewayRequest.objects.get(pk=request_id)
    assert row.approved_by_id == reviewer.pk


def test_review_approval_materialises_the_farm_from_farm_spec(
    provisioning_world, django_user_model
):
    """AD-15: the request may CREATE the farm — the org rep enters its initial
    information before it exists."""
    w = provisioning_world
    reviewer = django_user_model.objects.create_user(username="reviewer-farm-spec", password="x")
    reviewer.is_platform_staff = True
    reviewer.staff_role = "admin"
    reviewer.save()

    request_id = _submit_request(
        w,
        payload={
            "farm_spec": {
                "name": "New Shahriar Farm",
                "province_id": w["province"].pk,
                "city_id": w["city"].pk,
            },
            "expected_gateways": 3,
        },
    )
    assert GatewayRequest.objects.get(pk=request_id).farm_id is None

    response = _request(
        review_gateway_request, "POST",
        f"/api/v1/gateway-requests/{request_id}/review/", reviewer, {},
        {"decision": "APPROVE"},
        request_id=request_id,
    )

    assert response.status_code == 200
    farm = Farm.objects.get(pk=response.data["farm_id"])
    assert farm.name == "New Shahriar Farm"
    assert farm.org_id == w["org"].org_id
    assert farm.province_id == w["province"].pk
    assert farm.city_id == w["city"].pk
    # Farm has no created_by; provenance is the request's requested_by.
    assert GatewayRequest.objects.get(pk=request_id).requested_by_id == w["user"].pk
    assert response.data["jobs_created"] == 3
    assert InstallationJob.objects.filter(request_id=request_id).count() == 3


@pytest.mark.parametrize("payload", [
    {},
    {"farm_id": None},
    {"farm_id": 1, "farm_spec": {"name": "x"}},
    {"farm_spec": {"name": ""}},
    {"farm_spec": {"province_id": 1}},
    {"farm_spec": {"name": "x", "province_id": 1}},          # city missing
    {"farm_spec": {"name": "x", "city_id": 1}},              # province missing
    {"farm_id": 1, "expected_gateways": 0},
    {"farm_id": 1, "expected_gateways": 99},
])
def test_request_body_is_validated(provisioning_world, payload):
    w = provisioning_world
    response = _request(
        gateway_requests, "POST", "/api/v1/gateway-requests/", w["user"],
        {"org_id": str(w["org"].org_id), "roles": ["manager"]},
        payload,
    )
    assert response.status_code == 400, f"{payload} should be rejected"


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
