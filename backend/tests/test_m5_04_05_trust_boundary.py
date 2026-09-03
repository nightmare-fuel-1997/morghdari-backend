"""M5-04/M5-05 acceptance — telemetry trust boundary and FM lifecycle.

M5-04: telemetry from a gateway that is not yet provisioned is HELD in the
FIFO `telemetry:pending_approval` stream (never published, never dropped); from a
REJECTED or SUSPENDED gateway it is ARCHIVED to the DLQ with a machine reason.
A gateway with no provisioning record (legacy/demo world) flows normally.

M5-05: farm-manager approve/reject/suspend/resume — each transition goes
through the shared atomic service (status + audit + cache invalidation) and
dispatches its broker-consistency step AFTER the commit: approve drains the
held FIFO into Postgres, reject archives held telemetry to the DLQ, suspend
revokes EMQX ACLs, resume re-syncs them.

فارسی: پذیرش M5-04/05 — تله‌متریِ پیش از تأیید در صفِ انتظار می‌ماند و پس از
رد/تعلیق با دلیل بایگانی می‌شود؛ تأیید مدیر مزرعه صف را تخلیه و رد، آن را به
صندوق مرده می‌فرستد.
"""

import json
import uuid

import pytest
import redis as redis_lib

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL  # noqa: E402
from tests.conftest import redis_base_url  # noqa: E402

from apps.accounts.models import OrgMember  # noqa: E402
from apps.devices.models import GatewaySlot  # noqa: E402
from apps.farms.models import Farm  # noqa: E402
from apps.geography.models import City, Province  # noqa: E402
from apps.provisioning.api import (  # noqa: E402
    approve_gateway,
    reject_gateway,
    resume_gateway,
    suspend_gateway,
)
from apps.provisioning.models import ProvisioningAuditLog  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402
from apps.mqtt_auth.models import MqttDeviceCredential  # noqa: E402
from morghdari.subscriber.dlq import DLQ_STREAM  # noqa: E402
from morghdari.subscriber.pipeline import (  # noqa: E402
    STREAM_HISTORICAL,
    STREAM_PENDING,
    STREAM_REALTIME,
    handle_message,
)

SCRATCH_REDIS_URL = redis_base_url() + "/12"


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    """Isolated logical DBs for THIS module; every service lazily building a
    Redis client gets the same integration client injected."""
    scratch = redis_lib.Redis.from_url(SCRATCH_REDIS_URL, decode_responses=True)
    scratch.flushdb()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    yield client


def _payload(gateway_slot: str, device_ts: str) -> bytes:
    return json.dumps({
        "gateway_hardware_id": f"HW-{gateway_slot}",
        "gateway_slot_id": gateway_slot,
        "schema_version": "2.0",
        "device_ts": device_ts,
        "is_buffered": False,
        "readings": [{"pack_id": "P-A", "metrics": {"temp": {"avg": 28.0}}}],
    }).encode()


def _topic(org_id, gateway_slot: str) -> str:
    return f"{org_id}/farms/1/gateways/{gateway_slot}/telemetry"


def _publish(client, org_id, gateway_slot, device_ts, status=None):
    """Publish via the real pipeline; `status` drives the M5-04 branch."""
    return handle_message(
        client,
        _topic(org_id, gateway_slot),
        _payload(gateway_slot, device_ts),
        db_fetch=lambda *a: f"slot-{gateway_slot}",
        status_fetch=(lambda gw: status) if status is not None else None,
    )


def _range(client, stream):
    return client.xrange(stream, min="-", max="+")


# ---------------------------------------------------------------------------
# M5-04 trust boundary — routing by provisioning status
# ---------------------------------------------------------------------------


def test_pending_approval_telemetry_is_held_not_published(_fresh_redis):
    client = _fresh_redis
    assert _publish(client, "org-1", "GW-HOLD", "2026-08-01T10:00:00Z", "PENDING_APPROVAL") is None

    pending = _range(client, STREAM_PENDING)
    assert len(pending) == 1
    assert pending[0][1]["gateway_slot_id"] == "GW-HOLD"
    # never allowed into the realtime/historical lanes
    assert _range(client, STREAM_REALTIME) == []
    assert _range(client, STREAM_HISTORICAL) == []


def test_pending_buffer_is_fifo(_fresh_redis):
    client = _fresh_redis
    for i in range(3):
        _publish(client, "org-1", "GW-FIFO", f"2026-08-01T10:00:0{i}Z", "PENDING_APPROVAL")
    entries = [entry[1]["device_ts"] for entry in _range(client, STREAM_PENDING)]
    assert entries == [
        "2026-08-01T10:00:00Z",
        "2026-08-01T10:00:01Z",
        "2026-08-01T10:00:02Z",
    ]


def test_rejected_gateway_telemetry_is_archived_to_dlq(_fresh_redis):
    client = _fresh_redis
    entry_id = _publish(client, "org-1", "GW-DENIED", "2026-08-01T10:00:00Z", "REJECTED")
    assert entry_id is not None  # a DLQ entry id, not None

    dlq = _range(client, DLQ_STREAM)
    assert len(dlq) == 1
    fields = dlq[0][1]
    assert fields["topic"] == _topic("org-1", "GW-DENIED")
    assert fields["reason"] == "gateway_rejected"
    # and nothing reached any live lane
    assert _range(client, STREAM_PENDING) == []
    assert _range(client, STREAM_REALTIME) == []
    assert _range(client, STREAM_HISTORICAL) == []


def test_suspended_gateway_telemetry_is_archived_to_dlq(_fresh_redis):
    client = _fresh_redis
    entry_id = _publish(client, "org-1", "GW-OFF", "2026-08-01T10:00:00Z", "SUSPENDED")
    assert entry_id is not None

    dlq = _range(client, DLQ_STREAM)
    assert dlq[0][1]["reason"] == "gateway_suspended"


def test_legacy_gateway_without_record_flows_normally(_fresh_redis):
    client = _fresh_redis
    assert _publish(client, "org-1", "GW-LEGACY", "2026-08-01T10:00:00Z") is None
    realtime = _range(client, STREAM_REALTIME)
    assert len(realtime) == 1
    assert realtime[0][1]["gateway_slot_id"] == "GW-LEGACY"
    assert _range(client, STREAM_PENDING) == []


def test_provisioned_gateway_flows_normally(_fresh_redis):
    client = _fresh_redis
    _publish(client, "org-1", "GW-OK", "2026-08-01T10:00:00Z", "PROVISIONED")
    assert len(_range(client, STREAM_REALTIME)) == 1


def test_discard_pending_archives_only_target_gateway(_fresh_redis, monkeypatch):
    from apps.telemetry.consumers import discard_pending_for_gateway

    client = _fresh_redis
    _publish(client, "org-1", "GW-A", "2026-08-01T10:00:00Z", "PENDING_APPROVAL")
    _publish(client, "org-1", "GW-A", "2026-08-01T10:00:01Z", "PENDING_APPROVAL")
    _publish(client, "org-1", "GW-B", "2026-08-01T10:00:00Z", "PENDING_APPROVAL")

    archived = discard_pending_for_gateway(client, "GW-A", reason="gateway_rejected")

    assert archived == 2
    # GW-B's held telemetry is untouched
    remaining = _range(client, STREAM_PENDING)
    assert len(remaining) == 1
    assert remaining[0][1]["gateway_slot_id"] == "GW-B"
    # GW-A's entries archived with the reason, none of GW-B's
    reasons = [entry[1]["reason"] for entry in _range(client, DLQ_STREAM)]
    assert reasons == ["gateway_rejected", "gateway_rejected"]
    topics = [entry[1]["topic"] for entry in _range(client, DLQ_STREAM)]
    assert all("GW-A" in topic for topic in topics)


# ---------------------------------------------------------------------------
# M5-05 farm-manager lifecycle API
# ---------------------------------------------------------------------------


@pytest.fixture
def m5_world(django_user_model):
    tag = uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    manager = django_user_model.objects.create_user(username=f"mgr-{tag}", password="x")
    OrgMember.objects.create(user=manager, org=org, role="manager", is_active=True)
    worker = django_user_model.objects.create_user(username=f"wkr-{tag}", password="x")
    OrgMember.objects.create(user=worker, org=org, role="worker", is_active=True)
    slot = GatewaySlot.objects.create(
        slot_id=f"GW-{tag}",
        org=org,
        farm=farm,
        created_by=manager,
        provisioning_status="PENDING_APPROVAL",
    )
    other_org = Organization.objects.create(name=f"other-{tag}")
    other_manager = django_user_model.objects.create_user(username=f"omgr-{tag}", password="x")
    OrgMember.objects.create(user=other_manager, org=other_org, role="manager", is_active=True)
    claims = {"org_id": str(org.org_id), "roles": ["manager"]}
    worker_claims = {"org_id": str(org.org_id), "roles": ["worker"]}
    other_claims = {"org_id": str(other_org.org_id), "roles": ["manager"]}
    return locals()


def _api_call(view, user, claims, path, data=None):
    """Call an api_view function directly; URL kwargs come from the path
    (DRF does not re-parse the URL when the function is invoked directly)."""
    from rest_framework.test import APIRequestFactory, force_authenticate

    request = APIRequestFactory().post(path, data=data or {}, format="json")
    force_authenticate(request, user=user)
    request.jwt_claims = claims
    parts = path.split("/")
    slot_id = parts[parts.index("gateways") + 1]
    return view(request, slot_id=slot_id)


@pytest.mark.django_db
def test_manager_approve_drains_and_audits(m5_world, monkeypatch, _fresh_redis):
    w = m5_world
    called = {}
    monkeypatch.setattr(
        "apps.telemetry.consumers.drain_stream",
        lambda *args, **kw: called.update({"stream": args[0], "gateway_slot": kw.get("gateway_slot")}) or 2,
    )
    monkeypatch.setattr("apps.mqtt_auth.services.sync_gateway_acls",
                        lambda slot: {"synced": 1, "reachable": True})

    response = _api_call(approve_gateway, w["manager"], w["claims"], f"/api/v1/gateways/{w['slot'].slot_id}/approve/")

    assert response.status_code == 200
    assert response.data["status"] == "PROVISIONED"
    w["slot"].refresh_from_db()
    assert w["slot"].provisioning_status == "PROVISIONED"
    # audit row records the lifecycle event
    audit = ProvisioningAuditLog.objects.get(gateway_slot=w["slot"])
    assert audit.previous_status == "PENDING_APPROVAL"
    assert audit.new_status == "PROVISIONED"
    # drain dispatch targeted THIS gateway on the pending stream
    assert called == {"stream": STREAM_PENDING, "gateway_slot": w["slot"].slot_id}


@pytest.mark.django_db
def test_manager_reject_archives_held_telemetry(m5_world, monkeypatch, _fresh_redis):
    from apps.mqtt_auth.services import revoke_gateway_acls

    w = m5_world
    client = _fresh_redis
    _publish(client, str(w["org"].org_id), w["slot"].slot_id, "2026-08-01T10:00:00Z", "PENDING_APPROVAL")
    monkeypatch.setattr("apps.mqtt_auth.services.revoke_gateway_acls", lambda slot: True)

    response = _api_call(
        reject_gateway, w["manager"], w["claims"],
        f"/api/v1/gateways/{w['slot'].slot_id}/reject/",
        {"reason": "forged hardware"},
    )

    assert response.status_code == 200
    assert response.data["status"] == "REJECTED"
    # held telemetry archived to the DLQ with a reason, pending emptied
    dlq = _range(client, DLQ_STREAM)
    assert len(dlq) == 1
    assert dlq[0][1]["reason"] == "gateway_rejected"
    assert _range(client, STREAM_PENDING) == []
    audit = ProvisioningAuditLog.objects.get(gateway_slot=w["slot"])
    assert audit.new_status == "REJECTED"
    assert audit.reason == "forged hardware"


@pytest.mark.django_db
def test_suspend_then_resume_cycle(m5_world, monkeypatch):
    import apps.mqtt_auth.services as svc

    w = m5_world
    w["slot"].provisioning_status = "PROVISIONED"
    w["slot"].save(update_fields=["provisioning_status"])
    MqttDeviceCredential.objects.create(
        device_id=w["slot"].slot_id,
        username=w["slot"].slot_id,
        password_hash="$2b$12$abcdefghijklmnopqrstuvABCDEFGHIJKLMNOPQRSTUV",
    )
    synced, revoked = [], []
    monkeypatch.setattr(svc, "sync_gateway_acls", lambda slot: synced.append(slot.slot_id) or {"synced": 1, "reachable": True})
    monkeypatch.setattr(svc, "revoke_gateway_acls", lambda slot: revoked.append(slot.slot_id) or True)

    r1 = _api_call(suspend_gateway, w["manager"], w["claims"], f"/api/v1/gateways/{w['slot'].slot_id}/suspend/")
    assert r1.status_code == 200
    assert r1.data["status"] == "SUSPENDED"
    assert revoked == [w["slot"].slot_id]  # ACL revocation dispatched

    r2 = _api_call(resume_gateway, w["manager"], w["claims"], f"/api/v1/gateways/{w['slot'].slot_id}/resume/")
    assert r2.status_code == 200
    assert r2.data["status"] == "PROVISIONED"
    assert synced == [w["slot"].slot_id]  # allow-rules re-synced


@pytest.mark.django_db
def test_worker_cannot_lifecycle(m5_world, monkeypatch):
    w = m5_world
    response = _api_call(approve_gateway, w["worker"], w["worker_claims"], f"/api/v1/gateways/{w['slot'].slot_id}/approve/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_other_org_manager_gets_404(m5_world, monkeypatch):
    w = m5_world
    response = _api_call(approve_gateway, w["other_manager"], w["other_claims"], f"/api/v1/gateways/{w['slot'].slot_id}/approve/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_wrong_state_transitions_conflict(m5_world, monkeypatch):
    from apps.mqtt_auth.services import revoke_gateway_acls

    w = m5_world
    w["slot"].provisioning_status = "PROVISIONED"
    w["slot"].save(update_fields=["provisioning_status"])
    monkeypatch.setattr("apps.mqtt_auth.services.revoke_gateway_acls", lambda slot: True)

    # Blueprint §11: REJECTED may only come from PENDING_APPROVAL — rejecting a
    # PROVISIONED gateway is not a legal transition.
    response = _api_call(reject_gateway, w["manager"], w["claims"], f"/api/v1/gateways/{w['slot'].slot_id}/reject/")
    assert response.status_code == 409
    w["slot"].refresh_from_db()
    assert w["slot"].provisioning_status == "PROVISIONED"  # no partial write
    assert ProvisioningAuditLog.objects.filter(gateway_slot=w["slot"]).count() == 0