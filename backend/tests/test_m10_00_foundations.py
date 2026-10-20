"""Phase 0 acceptance — foundations for the MQTT-native provisioning flow.

Every assertion here maps to a decision in `docs/mqtt-provisioning-redesign-v1.md`.
The point of this module is that Phase 0 changes NO behaviour: it proves the new
tables, the publisher, the topic grammar, the desired-state document, and the
reconciler all work in isolation, before any endpoint starts using them.
"""

import json

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.session_tokens import (
    KIND_INSTALLER,
    KIND_SUPPORT,
    SESSION_TTL_SEC,
    issue_scoped_session,
)
from apps.accounts.system import SYSTEM_USERNAME, get_system_user
from apps.devices.models import (
    DevicePhoto,
    DeviceUnit,
    GatewaySlot,
    SaloonSensorSlot,
    GatewaySensorMapping,
)
from apps.mqtt_auth import publisher
from apps.provisioning import topics
from apps.provisioning.desired_state import build_desired_state
from apps.provisioning.models import GatewayCommand, InstallationJob
from apps.provisioning.tasks import reconcile_reported
from apps.tenants.models import Organization

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _org(name="phase0-org"):
    return Organization.objects.create(name=name)


def _slot(org, slot_id="GW-P0-1", **kwargs):
    return GatewaySlot.objects.create(
        slot_id=slot_id, org=org, provisioning_status="PENDING_INSTALLATION", **kwargs
    )


# ---------------------------------------------------------------------------
# AD-1: the factory inventory is org-less and its state machine is closed
# ---------------------------------------------------------------------------

def test_device_unit_has_no_org_column():
    """AD-1: the whole reason this table exists is that it carries no tenant."""
    field_names = {f.name for f in DeviceUnit._meta.get_fields()}
    assert "org" not in field_names
    assert "org_id" not in field_names


def test_device_unit_state_check_rejects_unknown_value():
    DeviceUnit.objects.create(unit_id="U-1", hardware_id="AA:BB:CC:00:01")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DeviceUnit.objects.create(
                unit_id="U-2", hardware_id="AA:BB:CC:00:02", state="NOPE"
            )


def test_device_unit_hardware_id_is_unique():
    DeviceUnit.objects.create(unit_id="U-3", hardware_id="AA:BB:CC:00:03")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DeviceUnit.objects.create(unit_id="U-4", hardware_id="AA:BB:CC:00:03")


# ---------------------------------------------------------------------------
# AD-16 / AD-21: one job per gateway, kinds and states closed
# ---------------------------------------------------------------------------

def test_installation_job_rejects_unknown_kind_and_state():
    org = _org("job-org")
    from apps.farms.models import Farm
    from apps.geography.models import City, Province

    province = Province.objects.create(name="Tehran-P0")
    city = City.objects.create(province=province, name="Varamin-P0")
    farm = Farm.objects.create(org=org, name="F", province=province, city=city)

    InstallationJob.objects.create(org=org, farm=farm, kind="INSTALL")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            InstallationJob.objects.create(org=org, farm=farm, kind="SOMETHING")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            InstallationJob.objects.create(org=org, farm=farm, state="NOWHERE")


def test_installation_job_unit_is_one_to_one():
    """A physical unit can never sit on two jobs at once."""
    org = _org("job-org-2")
    from apps.farms.models import Farm
    from apps.geography.models import City, Province

    province = Province.objects.create(name="Tehran-P0-2")
    city = City.objects.create(province=province, name="Varamin-P0-2")
    farm = Farm.objects.create(org=org, name="F2", province=province, city=city)
    unit = DeviceUnit.objects.create(unit_id="U-5", hardware_id="AA:BB:CC:00:05")

    InstallationJob.objects.create(org=org, farm=farm, unit=unit)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            InstallationJob.objects.create(org=org, farm=farm, unit=unit)


# ---------------------------------------------------------------------------
# AD-20: a photo targets exactly one device
# ---------------------------------------------------------------------------

def test_device_photo_requires_exactly_one_target(django_user_model):
    org = _org("photo-org")
    user = django_user_model.objects.create_user(username="p0-photo", password="x")
    slot = _slot(org, "GW-P0-PHOTO")

    # exactly one target → fine
    DevicePhoto.objects.create(
        org=org, gateway_slot=slot, uploaded_by=user, file_url="/media/a.jpg"
    )
    # neither target → rejected
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DevicePhoto.objects.create(org=org, uploaded_by=user, file_url="/media/b.jpg")


def test_device_photo_rejects_two_targets(django_user_model):
    org = _org("photo-org-2")
    user = django_user_model.objects.create_user(username="p0-photo2", password="x")
    slot = _slot(org, "GW-P0-PHOTO2")
    from apps.farms.models import Farm, Saloon
    from apps.geography.models import City, Province

    province = Province.objects.create(name="Tehran-P0-PH")
    city = City.objects.create(province=province, name="Varamin-P0-PH")
    farm = Farm.objects.create(org=org, name="FP", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="S1")
    from apps.devices.models import SaloonPosition

    position = SaloonPosition.objects.create(org=org, saloon=saloon, label="North")
    sensor = SaloonSensorSlot.objects.create(
        slot_id="SS-P0", org=org, saloon=saloon, position=position
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DevicePhoto.objects.create(
                org=org, gateway_slot=slot, sensor_slot=sensor,
                uploaded_by=user, file_url="/media/c.jpg",
            )


# ---------------------------------------------------------------------------
# AD-19: the device-lane topic grammar
# ---------------------------------------------------------------------------

def test_device_topic_round_trip():
    assert topics.desired_topic("GW-1") == "devices/GW-1/desired"
    assert topics.reported_topic("GW-1") == "devices/GW-1/reported"
    assert topics.command_topic("GW-1") == "devices/GW-1/command"
    assert topics.parse_device_topic("devices/GW-1/reported") == ("GW-1", "reported")


@pytest.mark.parametrize(
    "bad",
    ["", "devices/GW-1", "devices/GW-1/telemetry", "org/farms/1/gateways/GW/telemetry",
     "devices/GW-1/reported/extra"],
)
def test_device_topic_rejects_foreign_shapes(bad):
    with pytest.raises(topics.DeviceTopicError):
        topics.parse_device_topic(bad)


# ---------------------------------------------------------------------------
# E1: the publisher never raises, and degrades to "not configured"
# ---------------------------------------------------------------------------

class _FakeInfo:
    rc = 0

    def __init__(self, published=True):
        self._published = published

    def wait_for_publish(self, timeout=None):
        return None

    def is_published(self):
        return self._published


class _FakeClient:
    def __init__(self, info=None):
        self.calls = []
        self._info = info or _FakeInfo()

    def publish(self, topic, payload, qos=0, retain=False):
        self.calls.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return self._info


def test_publisher_returns_false_when_not_configured(monkeypatch):
    monkeypatch.delenv("MQTT_HOST", raising=False)
    publisher.reset()
    assert publisher.publish("devices/X/desired", {"a": 1}, retain=True) is False


def test_publisher_publishes_json_with_qos1_and_retain():
    fake = _FakeClient()
    publisher.set_client(fake)
    try:
        assert publisher.publish("devices/X/desired", {"a": 1}, retain=True) is True
    finally:
        publisher.reset()
    call = fake.calls[0]
    assert call["topic"] == "devices/X/desired"
    assert call["qos"] == 1
    assert call["retain"] is True
    assert json.loads(call["payload"]) == {"a": 1}


def test_publisher_returns_false_when_not_acknowledged():
    publisher.set_client(_FakeClient(_FakeInfo(published=False)))
    try:
        assert publisher.publish("devices/X/desired", {"a": 1}) is False
    finally:
        publisher.reset()


# ---------------------------------------------------------------------------
# AD-3/AD-4: the desired-state document
# ---------------------------------------------------------------------------

def _world_with_sensor(org, slot_id="GW-P0-DS"):
    from apps.farms.models import Farm, Saloon
    from apps.geography.models import City, Province
    from apps.devices.models import SaloonPosition

    province = Province.objects.create(name=f"Prov-{slot_id}")
    city = City.objects.create(province=province, name=f"City-{slot_id}")
    farm = Farm.objects.create(org=org, name="Desired Farm", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="Saloon 1")
    position = SaloonPosition.objects.create(org=org, saloon=saloon, label="North Wall")
    sensor = SaloonSensorSlot.objects.create(
        slot_id=f"SS-{slot_id}", org=org, saloon=saloon, position=position,
        allowed_metric_keys=["temp", "humidity"],
    )
    slot = GatewaySlot.objects.create(
        slot_id=slot_id, org=org, farm=farm, provisioning_status="PENDING_INSTALLATION",
        desired_config_version=3,
    )
    # The new flow's hardware identity lives on the bound factory unit (AD-2);
    # a legacy slot would carry a gateway_hardware_registry row instead. Both
    # resolve through desired_state.gateway_hardware_id().
    DeviceUnit.objects.create(
        unit_id=slot_id, hardware_id="AA:BB:CC:DD:EE:FF",
        state="BOUND", slot=slot,
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id="AA:BB:CC:DD:EE:FF", pack_id="a1b2c3d4",
        slot=sensor, org=org,
    )
    return slot, sensor


def test_desired_state_is_complete_and_leaks_no_secrets():
    org = _org("ds-org")
    slot, _sensor = _world_with_sensor(org)

    doc = build_desired_state(slot)

    assert doc["schema_version"] == "1.0"
    assert doc["config_version"] == 3
    assert doc["slot_id"] == slot.slot_id
    assert str(doc["org_id"]) == str(org.org_id)
    assert doc["farm_id"] == slot.farm_id
    assert doc["gateway_hardware_id"] == "AA:BB:CC:DD:EE:FF"
    assert doc["topics"]["desired"] == f"devices/{slot.slot_id}/desired"

    assert len(doc["sensors"]) == 1
    sensor = doc["sensors"][0]
    assert sensor["pack_id"] == "a1b2c3d4"
    assert sensor["saloon_name"] == "Saloon 1"
    assert sensor["position"] == "North Wall"
    assert sensor["metrics"] == ["temp", "humidity"]

    blob = json.dumps(doc)
    assert "$2b$" not in blob
    assert "password" not in blob.lower()
    assert "claim_code" not in blob


def test_desired_state_sensors_are_deterministically_ordered():
    org = _org("ds-org-order")
    slot, _sensor = _world_with_sensor(org, "GW-P0-ORD")
    # A second pack on the same gateway, earlier alphabetically.
    from apps.devices.models import GatewaySensorMapping, SaloonSensorSlot

    other = SaloonSensorSlot.objects.create(
        slot_id="SS-ORD-2", org=org, saloon=_sensor.saloon, position=_sensor.position,
        allowed_metric_keys=["nh3"],
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id="AA:BB:CC:DD:EE:FF", pack_id="0000aaaa", slot=other, org=org,
    )
    doc = build_desired_state(slot)
    assert [s["pack_id"] for s in doc["sensors"]] == ["0000aaaa", "a1b2c3d4"]


# ---------------------------------------------------------------------------
# AD-17: the reconciler is the technical gate that replaced FM approval
# ---------------------------------------------------------------------------

@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_reported_applied_activates_a_submitted_job(django_user_model):
    org = _org("rec-org")
    slot, _sensor = _world_with_sensor(org, "GW-P0-REC")
    slot.desired_config_version = 1
    slot.applied_config_version = 0
    slot.save(update_fields=["desired_config_version", "applied_config_version"])

    job = InstallationJob.objects.create(
        org=org, farm_id=slot.farm_id, gateway_slot=slot, state="SUBMITTED",
    )

    outcome = reconcile_reported(
        {"slot_id": slot.slot_id, "config_version": 1, "state": "APPLIED"},
        topic_slot_id=slot.slot_id,
    )

    assert outcome == "APPLIED"
    slot.refresh_from_db()
    job.refresh_from_db()
    assert slot.applied_config_version == 1
    assert slot.applied_at is not None
    assert slot.provisioning_status == "PROVISIONED"
    assert job.state == "COMPLETED"


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_reported_applied_does_not_activate_without_a_submitted_job():
    """The gate is `applied == desired` AND submitted — never one of them."""
    org = _org("rec-org-2")
    slot, _sensor = _world_with_sensor(org, "GW-P0-REC2")
    slot.desired_config_version = 1
    slot.save(update_fields=["desired_config_version"])

    assert reconcile_reported(
        {"slot_id": slot.slot_id, "config_version": 1, "state": "APPLIED"}
    ) == "APPLIED"
    slot.refresh_from_db()
    assert slot.provisioning_status == "PENDING_INSTALLATION"


def test_reported_ignores_a_stale_version():
    org = _org("rec-org-3")
    slot, _sensor = _world_with_sensor(org, "GW-P0-REC3")
    slot.applied_config_version = 5
    slot.save(update_fields=["applied_config_version"])

    assert reconcile_reported(
        {"slot_id": slot.slot_id, "config_version": 4, "state": "APPLIED"}
    ) == "IGNORED_STALE"
    slot.refresh_from_db()
    assert slot.applied_config_version == 5


def test_reported_rejects_identity_mismatch():
    """The topic segment is unforgeable; a disagreeing payload is a fault."""
    assert reconcile_reported(
        {"slot_id": "GW-A", "config_version": 1, "state": "APPLIED"},
        topic_slot_id="GW-B",
    ) == "IDENTITY_MISMATCH"


def test_reported_unknown_slot_is_not_a_crash():
    assert reconcile_reported(
        {"slot_id": "GW-DOES-NOT-EXIST", "config_version": 1, "state": "APPLIED"}
    ) == "UNKNOWN_SLOT"


# ---------------------------------------------------------------------------
# AD-17: the system actor
# ---------------------------------------------------------------------------

def test_system_user_is_created_once_and_cannot_log_in():
    first = get_system_user()
    second = get_system_user()
    assert first.pk == second.pk
    assert first.username == SYSTEM_USERNAME
    assert first.is_active is False
    assert first.is_platform_staff is False
    assert first.has_usable_password() is False


# ---------------------------------------------------------------------------
# AD-14: the two session kinds must stay distinct
# ---------------------------------------------------------------------------

def test_installer_session_carries_scope_but_not_the_support_claim(django_user_model):
    org = _org("session-org")
    user = django_user_model.objects.create_user(username="p0-installer", password="x")

    from rest_framework_simplejwt.tokens import AccessToken

    token = AccessToken(
        issue_scoped_session(
            user, org_id=org.org_id, kind=KIND_INSTALLER,
            extra_claims={"job_id": 7, "farm_id": 12},
        )
    )
    assert token["installer_session"] is True
    assert "support_session" not in token
    assert token["org_id"] == str(org.org_id)
    assert token["job_id"] == 7
    assert token["farm_id"] == 12


def test_support_session_does_not_carry_the_installer_claim(django_user_model):
    org = _org("session-org-2")
    user = django_user_model.objects.create_user(username="p0-support", password="x")

    from rest_framework_simplejwt.tokens import AccessToken

    token = AccessToken(
        issue_scoped_session(
            user, org_id=org.org_id, kind=KIND_SUPPORT,
            extra_claims={"incident_id": "INC-1"},
        )
    )
    assert token["support_session"] is True
    assert "installer_session" not in token
    assert token["incident_id"] == "INC-1"


def test_session_ttl_is_fifteen_minutes(django_user_model):
    org = _org("session-org-3")
    user = django_user_model.objects.create_user(username="p0-ttl", password="x")

    from rest_framework_simplejwt.tokens import AccessToken

    token = AccessToken(issue_scoped_session(user, org_id=org.org_id, kind=KIND_INSTALLER))
    assert int(token["exp"]) - int(token["iat"]) == SESSION_TTL_SEC
