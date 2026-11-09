"""Option A+1 Test Suite — Complete verification of tenant isolation,
pipeline gate ordering, Redis metadata caching, and D21 ACL unification.

23 Comprehensive Test Cases:
  Cases 1–8:   Ownership validation (valid, unknown, cross-org RLS, farm mismatch,
               unassigned farm, suspended, rejected, payload slot mismatch)
  Cases 9–12:  Ingestion ordering (rejected before diag, before pack resolution,
               no stream entry, valid continues)
  Cases 13–17: Cache isolation & invalidation (tenant-scoped keys, cross-org
               isolation, farm change invalidation, transition invalidation,
               negative cache TTL)
  Cases 18–23: ACL lifecycle & D21 fix (unbound 4 rules, bound 13 rules, suspend
               deletes, resume restores 13 rules, binding creates 13 rules, zero
               wildcards)
"""

import json
import uuid
import pytest
from django.test import override_settings

from apps.devices.models import GatewaySlot, GatewayRequest
from apps.farms.models import Farm
from apps.geography.models import Province, City
from apps.mqtt_auth.models import MqttAclRule
from apps.mqtt_auth.services import (
    canonical_gateway_acl_rules,
    bootstrap_acl_rule_items,
    sync_gateway_acls,
)
from apps.provisioning.installations import _bind_job_to_unit
from apps.provisioning.models import InstallationJob
from apps.provisioning.services import (
    transition,
    suspend_gateway,
    resume_gateway,
    _restore_db_acl_rules,
)
from apps.tenants.models import Organization
from morghdari.subscriber.dlq import DLQ_STREAM
from morghdari.subscriber.main import _fetch_gateway_meta
from morghdari.subscriber.pipeline import (
    handle_message,
    STREAM_REALTIME,
    STREAM_HISTORICAL,
    STREAM_PENDING,
)


class MockRedis:
    """Hermetic in-memory Redis stand-in supporting streams, string keys, and TTLs."""

    def __init__(self):
        self.data = {}
        self.ttls = {}
        self.streams = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = str(value) if not isinstance(value, str) else value
        if ex:
            self.ttls[key] = ex
        return True

    def delete(self, *keys):
        count = 0
        for k in keys:
            if k in self.data:
                del self.data[k]
                self.ttls.pop(k, None)
                count += 1
        return count

    def incr(self, key):
        val = int(self.data.get(key, 0)) + 1
        self.data[key] = str(val)
        return val

    def xadd(self, stream, fields):
        if stream not in self.streams:
            self.streams[stream] = []
        entry_id = f"{len(self.streams[stream]) + 1}-0"
        self.streams[stream].append((entry_id, fields))
        return entry_id

    def xtrim(self, *args, **kwargs):
        return 0

    def xrange(self, stream, min="-", max="+"):
        return self.streams.get(stream, [])


def _make_payload(slot_id: str, device_ts="2026-11-09T10:00:00Z", is_diag=False, readings=None):
    if readings is None:
        readings = [] if is_diag else [{"pack_id": "P-A", "metrics": {"temp": {"avg": 25.4}}}]
    return json.dumps({
        "schema_version": "2.0",
        "gateway_slot_id": slot_id,
        "gateway_hardware_id": f"HW-{slot_id}",
        "device_ts": device_ts,
        "is_buffered": False,
        "readings": readings,
    }).encode("utf-8")


@pytest.fixture
def fake_redis():
    return MockRedis()


@pytest.fixture
def geo_data(db):
    tag = uuid.uuid4().hex[:6]
    prov = Province.objects.create(name=f"Prov-{tag}")
    cty = City.objects.create(province=prov, name=f"City-{tag}")
    return prov, cty


@pytest.fixture
def org_and_farm(db, geo_data):
    prov, cty = geo_data
    org = Organization.objects.create(name="Org Alpha")
    farm = Farm.objects.create(org=org, name="Farm 1", province=prov, city=cty)
    return org, farm


@pytest.fixture
def test_user(db):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.create(username=f"test_user_{uuid.uuid4().hex[:6]}", email="admin@example.com")


# ===========================================================================
# Cases 1–8: Ownership validation
# ===========================================================================

@pytest.mark.django_db
def test_case_01_valid_telemetry_accepted_and_routed_realtime(fake_redis, org_and_farm):
    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-TEST-01", org=org, farm=farm, provisioning_status="PROVISIONED"
    )
    topic = f"{org.org_id}/farms/{farm.pk}/gateways/{slot.slot_id}/telemetry"
    payload = _make_payload(slot.slot_id)

    res = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
        db_fetch=lambda *a: "slot-resolved-1",
    )
    assert res is None  # Success
    realtime = fake_redis.xrange(STREAM_REALTIME)
    assert len(realtime) == 1
    assert realtime[0][1]["gateway_slot_id"] == slot.slot_id
    assert realtime[0][1]["org_id"] == str(org.org_id)
    assert realtime[0][1]["farm_id"] == str(farm.pk)


@pytest.mark.django_db
def test_case_02_unknown_gateway_rejected_to_dlq(fake_redis, org_and_farm):
    org, farm = org_and_farm
    topic = f"{org.org_id}/farms/{farm.pk}/gateways/GW-NONEXISTENT/telemetry"
    payload = _make_payload("GW-NONEXISTENT")

    dlq_id = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
    )
    assert dlq_id is not None
    dlq = fake_redis.xrange(DLQ_STREAM)
    assert len(dlq) == 1
    assert dlq[0][1]["reason"] == "UNKNOWN_OR_UNAUTHORIZED_SLOT"
    assert len(fake_redis.xrange(STREAM_REALTIME)) == 0


@pytest.mark.django_db
def test_case_03_cross_tenant_gateway_rejected_to_dlq_via_rls(fake_redis, org_and_farm, geo_data):
    org_a, farm_a = org_and_farm
    prov, cty = geo_data
    org_b = Organization.objects.create(name="Org Beta")
    farm_b = Farm.objects.create(org=org_b, name="Farm 2", province=prov, city=cty)
    slot_b = GatewaySlot.objects.create(
        slot_id="GW-SLOT-B", org=org_b, farm=farm_b, provisioning_status="PROVISIONED"
    )

    # Attacker crafts topic claiming GW-SLOT-B under Org A
    topic = f"{org_a.org_id}/farms/{farm_a.pk}/gateways/{slot_b.slot_id}/telemetry"
    payload = _make_payload(slot_b.slot_id)

    dlq_id = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
    )
    assert dlq_id is not None
    dlq = fake_redis.xrange(DLQ_STREAM)
    assert len(dlq) == 1
    assert dlq[0][1]["reason"] == "UNKNOWN_OR_UNAUTHORIZED_SLOT"


@pytest.mark.django_db
def test_case_04_farm_mismatch_rejected_to_dlq(fake_redis, org_and_farm, geo_data):
    org, farm = org_and_farm
    prov, cty = geo_data
    other_farm = Farm.objects.create(org=org, name="Farm 99", province=prov, city=cty)
    slot = GatewaySlot.objects.create(
        slot_id="GW-FARM-MISMATCH", org=org, farm=farm, provisioning_status="PROVISIONED"
    )

    # Claiming other_farm instead of slot's actual farm
    topic = f"{org.org_id}/farms/{other_farm.pk}/gateways/{slot.slot_id}/telemetry"
    payload = _make_payload(slot.slot_id)

    dlq_id = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
    )
    assert dlq_id is not None
    dlq = fake_redis.xrange(DLQ_STREAM)
    assert len(dlq) == 1
    assert dlq[0][1]["reason"] == "TOPIC_FARM_MISMATCH"


@pytest.mark.django_db
def test_case_05_unassigned_farm_rejected_to_dlq(fake_redis, org_and_farm):
    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-NO-FARM", org=org, farm=None, provisioning_status="PENDING_INSTALLATION"
    )
    topic = f"{org.org_id}/farms/1/gateways/{slot.slot_id}/telemetry"
    payload = _make_payload(slot.slot_id)

    dlq_id = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
    )
    assert dlq_id is not None
    dlq = fake_redis.xrange(DLQ_STREAM)
    assert len(dlq) == 1
    assert dlq[0][1]["reason"] == "UNASSIGNED_FARM"


@pytest.mark.django_db
def test_case_06_suspended_gateway_rejected_to_dlq(fake_redis, org_and_farm):
    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-SUSP", org=org, farm=farm, provisioning_status="SUSPENDED"
    )
    topic = f"{org.org_id}/farms/{farm.pk}/gateways/{slot.slot_id}/telemetry"
    payload = _make_payload(slot.slot_id)

    dlq_id = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
    )
    assert dlq_id is not None
    dlq = fake_redis.xrange(DLQ_STREAM)
    assert len(dlq) == 1
    assert dlq[0][1]["reason"] == "gateway_suspended"


@pytest.mark.django_db
def test_case_07_rejected_gateway_rejected_to_dlq(fake_redis, org_and_farm):
    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-REJ", org=org, farm=farm, provisioning_status="REJECTED"
    )
    topic = f"{org.org_id}/farms/{farm.pk}/gateways/{slot.slot_id}/telemetry"
    payload = _make_payload(slot.slot_id)

    dlq_id = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
    )
    assert dlq_id is not None
    dlq = fake_redis.xrange(DLQ_STREAM)
    assert len(dlq) == 1
    assert dlq[0][1]["reason"] == "gateway_rejected"


@pytest.mark.django_db
def test_case_08_payload_slot_mismatch_rejected_to_dlq(fake_redis, org_and_farm):
    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-AUTH-01", org=org, farm=farm, provisioning_status="PROVISIONED"
    )
    topic = f"{org.org_id}/farms/{farm.pk}/gateways/{slot.slot_id}/telemetry"
    # Payload claims GW-DIFFERENT
    payload = _make_payload("GW-DIFFERENT")

    dlq_id = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
    )
    assert dlq_id is not None
    dlq = fake_redis.xrange(DLQ_STREAM)
    assert len(dlq) == 1
    assert dlq[0][1]["reason"] == "TOPIC_PAYLOAD_IDENTITY_MISMATCH"


# ===========================================================================
# Cases 9–12: Ingestion ordering
# ===========================================================================

@pytest.mark.django_db
def test_case_09_invalid_gateway_rejected_before_capture_diag(fake_redis, org_and_farm):
    org, farm = org_and_farm
    topic = f"{org.org_id}/farms/{farm.pk}/gateways/GW-ATTACKER/telemetry"
    payload = _make_payload("GW-ATTACKER", is_diag=True)

    dlq_id = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
    )
    assert dlq_id is not None
    # Diagnostic key MUST NOT have been captured
    assert fake_redis.get("diag:last:GW-ATTACKER") is None


@pytest.mark.django_db
def test_case_10_invalid_gateway_rejected_before_resolve_pack_slot(fake_redis, org_and_farm):
    org, farm = org_and_farm
    topic = f"{org.org_id}/farms/{farm.pk}/gateways/GW-UNAUTH/telemetry"
    payload = _make_payload("GW-UNAUTH")

    lookup_calls = []

    def mock_db_fetch(org_id, hw_id, pack_id):
        lookup_calls.append((hw_id, pack_id))
        return "slot-x"

    dlq_id = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
        db_fetch=mock_db_fetch,
    )
    assert dlq_id is not None
    # resolver db_fetch must NEVER be invoked for an unauthorized gateway
    assert len(lookup_calls) == 0


@pytest.mark.django_db
def test_case_11_invalid_gateway_does_not_enter_realtime_or_pending_stream(fake_redis, org_and_farm):
    org, farm = org_and_farm
    topic = f"{org.org_id}/farms/{farm.pk}/gateways/GW-GHOST/telemetry"
    payload = _make_payload("GW-GHOST")

    handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
    )
    assert len(fake_redis.xrange(STREAM_REALTIME)) == 0
    assert len(fake_redis.xrange(STREAM_PENDING)) == 0
    assert len(fake_redis.xrange(STREAM_HISTORICAL)) == 0


@pytest.mark.django_db
def test_case_12_valid_gateway_continues_through_all_gates(fake_redis, org_and_farm):
    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-FULL-PIPELINE", org=org, farm=farm, provisioning_status="PROVISIONED"
    )
    topic = f"{org.org_id}/farms/{farm.pk}/gateways/{slot.slot_id}/telemetry"
    payload = _make_payload(slot.slot_id, device_ts="2026-11-09T10:15:00Z")

    res = handle_message(
        fake_redis, topic, payload,
        meta_fetch=lambda o, f, s: _fetch_gateway_meta(fake_redis, o, f, s),
        db_fetch=lambda *a: "slot-pack-1",
    )
    assert res is None
    # Check that realtime stream entry was created with flat contract fields
    realtime = fake_redis.xrange(STREAM_REALTIME)
    assert len(realtime) == 1
    fields = realtime[0][1]
    assert fields["gateway_slot_id"] == slot.slot_id
    assert fields["slot_id"] == "slot-pack-1"
    assert "trace_id" in fields
    assert "recv_ts" in fields


# ===========================================================================
# Cases 13–17: Cache isolation & invalidation
# ===========================================================================

@pytest.mark.django_db
def test_case_13_cache_keys_are_strictly_tenant_scoped(fake_redis, org_and_farm):
    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-CACHE-01", org=org, farm=farm, provisioning_status="PROVISIONED"
    )

    meta = _fetch_gateway_meta(fake_redis, str(org.org_id), farm.pk, slot.slot_id)
    assert meta is not None
    assert meta["farm_id"] == farm.pk
    assert meta["provisioning_status"] == "PROVISIONED"

    # Verify key shapes
    ident_key = f"gw:ident:{org.org_id}:{slot.slot_id}"
    status_key = f"gw:status:{slot.slot_id}"
    assert fake_redis.get(ident_key) is not None
    assert fake_redis.get(status_key) == "PROVISIONED"
    assert 300 <= fake_redis.ttls.get(ident_key) <= 360
    assert fake_redis.ttls.get(status_key) == 60


@pytest.mark.django_db
def test_case_14_cross_org_cache_isolation(fake_redis, org_and_farm):
    org_a, farm_a = org_and_farm
    org_b = Organization.objects.create(name="Org B Cache")
    slot = GatewaySlot.objects.create(
        slot_id="GW-ISOLATE", org=org_a, farm=farm_a, provisioning_status="PROVISIONED"
    )

    # Populate cache under Org A
    _fetch_gateway_meta(fake_redis, str(org_a.org_id), farm_a.pk, slot.slot_id)
    assert fake_redis.get(f"gw:ident:{org_a.org_id}:{slot.slot_id}") is not None

    # Querying under Org B must miss and write to Org B negative cache
    meta_b = _fetch_gateway_meta(fake_redis, str(org_b.org_id), farm_a.pk, slot.slot_id)
    assert meta_b is None
    assert fake_redis.get(f"gw:neg:{org_b.org_id}:{slot.slot_id}") == "1"
    # Org A cache remains untouched and valid!
    meta_a = _fetch_gateway_meta(fake_redis, str(org_a.org_id), farm_a.pk, slot.slot_id)
    assert meta_a is not None


@pytest.mark.django_db
def test_case_15_farm_change_invalidation(fake_redis, org_and_farm, geo_data):
    org, farm_1 = org_and_farm
    prov, cty = geo_data
    farm_2 = Farm.objects.create(org=org, name="Farm 2", province=prov, city=cty)
    slot = GatewaySlot.objects.create(
        slot_id="GW-FARM-CHG", org=org, farm=farm_1, provisioning_status="PROVISIONED"
    )

    # Initial cache
    meta1 = _fetch_gateway_meta(fake_redis, str(org.org_id), farm_1.pk, slot.slot_id)
    assert meta1["farm_id"] == farm_1.pk

    # Slot moves farm and cache is invalidated
    slot.farm = farm_2
    slot.save(update_fields=["farm"])
    fake_redis.delete(f"gw:ident:{org.org_id}:{slot.slot_id}")

    meta2 = _fetch_gateway_meta(fake_redis, str(org.org_id), farm_2.pk, slot.slot_id)
    assert meta2["farm_id"] == farm_2.pk


@pytest.mark.django_db
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_case_16_status_transition_invalidates_status_and_identity_caches(fake_redis, org_and_farm, test_user, monkeypatch):
    from apps.provisioning import services
    monkeypatch.setattr(services, "_redis", lambda: fake_redis)

    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-TRANS-INV", org=org, farm=farm, provisioning_status="PROVISIONED"
    )

    # Prime cache
    _fetch_gateway_meta(fake_redis, str(org.org_id), farm.pk, slot.slot_id)
    assert fake_redis.get(f"gw:status:{slot.slot_id}") == "PROVISIONED"

    # Transition
    transition(slot.slot_id, "SUSPENDED", test_user, reason="test transition")

    # Both status and identity keys must be invalidated
    assert fake_redis.get(f"gw:status:{slot.slot_id}") is None
    assert fake_redis.get(f"gw:ident:{org.org_id}:{slot.slot_id}") is None


@pytest.mark.django_db
def test_case_17_stale_cache_recovery_and_negative_cache_expiry(fake_redis, org_and_farm):
    org, farm = org_and_farm
    # Slot does not exist yet -> negative cache populated
    meta = _fetch_gateway_meta(fake_redis, str(org.org_id), farm.pk, "GW-LATE-CREATE")
    assert meta is None
    assert fake_redis.get(f"gw:neg:{org.org_id}:GW-LATE-CREATE") == "1"
    assert fake_redis.ttls.get(f"gw:neg:{org.org_id}:GW-LATE-CREATE") == 30

    # Simulate negative cache expiry
    fake_redis.delete(f"gw:neg:{org.org_id}:GW-LATE-CREATE")

    # Gateway is created
    GatewaySlot.objects.create(
        slot_id="GW-LATE-CREATE", org=org, farm=farm, provisioning_status="PROVISIONED"
    )
    meta_new = _fetch_gateway_meta(fake_redis, str(org.org_id), farm.pk, "GW-LATE-CREATE")
    assert meta_new is not None
    assert meta_new["provisioning_status"] == "PROVISIONED"


# ===========================================================================
# Cases 18–23: ACL lifecycle & D21 fix
# ===========================================================================

def test_case_18_canonical_builder_unbound_yields_4_device_rules():
    rules = canonical_gateway_acl_rules("GW-UNBOUND-123")
    assert len(rules) == 4
    topics = {r["topic"] for r in rules}
    assert "devices/GW-UNBOUND-123/desired" in topics
    assert "devices/GW-UNBOUND-123/command" in topics
    assert "devices/GW-UNBOUND-123/reported" in topics
    assert "devices/GW-UNBOUND-123/command/response" in topics
    assert all(r["permission"] == "allow" for r in rules)


def test_case_19_canonical_builder_bound_yields_exact_13_rules():
    rules = canonical_gateway_acl_rules("GW-BOUND-123", "org-uuid-1", 42)
    assert len(rules) == 13
    topics = {r["topic"] for r in rules}
    # 4 device lane topics
    assert "devices/GW-BOUND-123/desired" in topics
    assert "devices/GW-BOUND-123/command" in topics
    assert "devices/GW-BOUND-123/reported" in topics
    assert "devices/GW-BOUND-123/command/response" in topics
    # 9 tenant topics
    base = "org-uuid-1/farms/42/gateways/GW-BOUND-123"
    assert f"{base}/telemetry" in topics
    assert f"{base}/status" in topics
    assert f"{base}/commands" in topics
    assert f"{base}/commands/response" in topics
    assert f"{base}/clock/sync" in topics
    assert f"{base}/ota" in topics
    assert f"{base}/ota/response" in topics
    assert f"{base}/diagnostics/request" in topics
    assert f"{base}/diagnostics/response" in topics


@pytest.mark.django_db
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_case_20_suspend_deletes_all_db_acl_rules(org_and_farm, test_user, monkeypatch):
    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-SUSP-ACL", org=org, farm=farm, provisioning_status="PROVISIONED"
    )
    # Populate rules
    rules = canonical_gateway_acl_rules(slot.slot_id, str(org.org_id), farm.pk)
    for r in rules:
        MqttAclRule.objects.create(username=slot.slot_id, **r)
    assert MqttAclRule.objects.filter(username=slot.slot_id).count() == 13

    suspend_gateway(slot.slot_id, test_user, reason="test suspension")
    assert MqttAclRule.objects.filter(username=slot.slot_id).count() == 0


@pytest.mark.django_db
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_case_21_resume_restores_all_13_rules_fixing_d21(org_and_farm, test_user, monkeypatch):
    org, farm = org_and_farm
    slot = GatewaySlot.objects.create(
        slot_id="GW-RESUME-D21", org=org, farm=farm, provisioning_status="SUSPENDED"
    )

    resume_gateway(slot.slot_id, test_user)
    db_rules = list(MqttAclRule.objects.filter(username=slot.slot_id))
    assert len(db_rules) == 13
    topics = {r.topic for r in db_rules}

    # Verify device lane rules were restored (D21 defect resolution)
    assert f"devices/{slot.slot_id}/command/response" in topics
    assert f"devices/{slot.slot_id}/command" in topics
    assert f"devices/{slot.slot_id}/desired" in topics
    assert f"devices/{slot.slot_id}/reported" in topics

    # Verify tenant rules are present
    assert f"{org.org_id}/farms/{farm.pk}/gateways/{slot.slot_id}/telemetry" in topics


@pytest.mark.django_db
def test_case_22_binding_creates_all_13_canonical_rules(org_and_farm, test_user, fake_redis, monkeypatch):
    from apps.provisioning import services
    monkeypatch.setattr(services, "_redis", lambda: fake_redis)

    org, farm = org_and_farm
    from apps.devices.models import DeviceUnit

    unit = DeviceUnit.objects.create(
        unit_id="GW-BIND-13",
        hardware_id="HW-UNIT-13",
        claim_code_hash="CODE13",
    )
    req = GatewayRequest.objects.create(org=org, farm=farm, expected_gateways=1, requested_by=test_user)
    job = InstallationJob.objects.create(org=org, farm=farm, request=req)

    _bind_job_to_unit(job, unit, actor=test_user, via_override=False, reason="test bind")

    rules = MqttAclRule.objects.filter(username=unit.unit_id)
    assert rules.count() == 13
    topics = {r.topic for r in rules}
    assert f"devices/{unit.unit_id}/command/response" in topics
    assert f"{org.org_id}/farms/{farm.pk}/gateways/{unit.unit_id}/telemetry" in topics


def test_case_23_zero_wildcards_and_strict_actions_in_acl_rules():
    rules = canonical_gateway_acl_rules("GW-WILD-CHECK", "org-x", 10)
    for r in rules:
        assert "+" not in r["topic"], f"Wildcard '+' detected in topic: {r['topic']}"
        assert "#" not in r["topic"], f"Wildcard '#' detected in topic: {r['topic']}"
        assert r["permission"] == "allow"
        assert r["action"] in ("publish", "subscribe")
