"""M4-08 acceptance — replay sweep: retroactive, idempotent, never pushes.

TASKS acceptance (rewritten by Tracker §4l-Q6): test_replay_analysis_alert +
counter-triggers-at-zero green; `test_replay_rows_are_never_touched_by_sweep`
already lives in test_m4_05_escalation.py; the ws/pending assertion moved to
M5-04. The 300 s-style pipeline here: buffered payload raises
replay:pending:{gateway_slot} (pipeline.py), the historical drain counts it
down, and AT ZERO the sweep dispatches exactly once (replay:sweeping NX,
Blueprint §5) → REPLAY_ANALYSIS rows with batch_id = {slot}:{first_device_ts},
idempotent on re-run, NEVER pushed.

فارسی: پذیرش M4-08 — بازپخشِ تاریخی هشدارِ ساکن می‌سازد، دوبار اجرا تکرار نمی‌سازد
و هرگز اعلان نمی‌فرستد؛ رسیدنِ شمارنده به صفر سویپ را دقیقاً یک‌بار می‌راناند.
"""

import json
import logging
import uuid

import pytest
import redis as redis_lib

from apps.alerts.models import AlertInstance
from apps.alerts.services.redis_keys import (
    replay_pending,
    thresholds_hash,
)
from apps.alerts.services.replay import run_replay_sweep
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization
from morghdari.subscriber.pipeline import handle_message

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL, redis_base_url  # noqa: E402

# Dedicated scratch logical DB for this module: wiped per test.
SCRATCH_REDIS_URL = redis_base_url() + "/14"


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    scratch = redis_lib.Redis.from_url(SCRATCH_REDIS_URL, decode_responses=True)
    scratch.flushdb()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    monkeypatch.setattr("apps.telemetry.consumers.CONSUMERS_REDIS_URL", REDIS_URL)
    monkeypatch.setattr("apps.alerts.services.replay.system_redis", lambda: client)
    yield client


def _make_world():
    tag = "e8" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"a-{tag}", saloon=saloon, org=org, position=position
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot, org=org
    )
    rc = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    rc.hset(thresholds_hash(saloon.id), mapping={
        "temp_max": "30.00", "temp_min": "18.00",
        "nh3_enabled": "1", "operational_state": "ACTIVE_FLOCK",
    })
    return {
        "org": org, "farm": farm, "saloon": saloon, "slot": slot,
        "redis": rc,
        "topic": f"{org.org_id}/farms/1/gateways/GW-{tag}/telemetry",
        "hw": f"hw-{tag}", "gw": f"GW-{tag}",
    }


def _ingest(world, device_ts: str, temp_avg: float):
    payload = json.dumps({
        "gateway_hardware_id": world["hw"],
        "gateway_slot_id": world["gw"],
        "schema_version": "2.0",
        "device_ts": device_ts,
        "is_buffered": True,
        "readings": [{"pack_id": "P-A",
                      "metrics": {"temp": {"avg": temp_avg}}}],
    }).encode()
    return handle_message(world["redis"], world["topic"], payload,
                          db_fetch=lambda *a: world["slot"].slot_id)


def _replay_alerts(world):
    return AlertInstance.objects.filter(
        org_id=world["org"].org_id, source="REPLAY_ANALYSIS"
    )


# ── ① the named acceptance ───────────────────────────────────────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_replay_analysis_alert(timescale_db, _fresh_redis):
    """A fully-drained buffered backlog yields retroactive alert rows with
    source=REPLAY_ANALYSIS and batch_id = {slot}:{first_device_ts}, stamped
    with the WORST breaching value of the window (Testing Strategy §2.2)."""
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    _ingest(world, "2026-08-25T06:00:00Z", temp_avg=32.0)
    _ingest(world, "2026-08-25T06:01:00Z", temp_avg=33.5)
    assert int(_fresh_redis.get(replay_pending(world["gw"]))) == 2

    drain_stream("telemetry:historical", count=10)  # counter hits 0 → sweep

    alerts = _replay_alerts(world)
    assert alerts.count() == 1
    alert = alerts.get()
    assert alert.metric_type == "temp"
    assert float(alert.current_value) == 33.5  # worst, not the last
    assert alert.current_status == "ACTIVE_TIER_1"
    assert alert.batch_id.startswith(f"{world['slot'].slot_id}:")
    assert "2026-08-25T06:00:00" in alert.batch_id  # FIRST device_ts of window


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_counter_triggers_sweep_exactly_at_zero(timescale_db, _fresh_redis):
    """Blueprint §5: partial backlog (counter > 0) dispatches NOTHING; the
    drained-to-zero batch dispatches exactly once."""
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    _ingest(world, "2026-08-25T06:00:00Z", temp_avg=32.0)
    _ingest(world, "2026-08-25T06:01:00Z", temp_avg=32.5)

    drain_stream("telemetry:historical", count=1)  # 1 of 2 → counter 1
    assert int(_fresh_redis.get(replay_pending(world["gw"]))) == 1
    assert not _replay_alerts(world).exists()

    drain_stream("telemetry:historical", count=10)  # counter → 0 → sweep
    assert _replay_alerts(world).count() == 1
    # The NX dispatch marker is cleaned by the sweep's finally → re-armed.
    assert not _fresh_redis.exists(f"replay:sweeping:{world['gw']}")


# ── ② idempotency + window re-arm ────────────────────────────────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_replay_sweep_is_idempotent(timescale_db, _fresh_redis):
    """Re-running the sweep (manual re-kick, crash-recovery) never duplicates
    a covered window — batch_id = {slot}:{first_device_ts} is the seam."""
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    _ingest(world, "2026-08-25T06:00:00Z", temp_avg=32.0)
    _ingest(world, "2026-08-25T06:01:00Z", temp_avg=33.5)
    drain_stream("telemetry:historical", count=10)
    assert _replay_alerts(world).count() == 1

    assert run_replay_sweep(world["gw"]) == []
    assert run_replay_sweep() == []
    assert _replay_alerts(world).count() == 1


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_new_outage_window_rearms_batch_id(timescale_db, _fresh_redis):
    """A LATER outage's buffered rows open a NEW window (fresh batch_id) —
    the covered high-water mark must not swallow the next episode."""
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    _ingest(world, "2026-08-25T06:00:00Z", temp_avg=32.0)
    drain_stream("telemetry:historical", count=10)
    assert _replay_alerts(world).count() == 1

    _ingest(world, "2026-08-25T07:00:00Z", temp_avg=34.0)  # new outage, worse
    drain_stream("telemetry:historical", count=10)
    assert _replay_alerts(world).count() == 2
    batch_ids = sorted(a.batch_id for a in _replay_alerts(world))
    assert len(set(batch_ids)) == 2  # distinct windows, both preserved


# ── ③ never-notify (Q6) ──────────────────────────────────────────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_replay_rows_are_never_pushed(timescale_db, _fresh_redis, caplog):
    """Replay rows are historical review material: the sweep creates them but
    NEVER routes a notification (nobody's phone buzzes for the past)."""
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    _ingest(world, "2026-08-25T06:00:00Z", temp_avg=32.0)
    drain_stream("telemetry:historical", count=10)
    assert _replay_alerts(world).count() == 1

    with caplog.at_level(logging.INFO, logger="apps.alerts.services.sweeps"):
        run_replay_sweep(world["gw"])
    assert not any("ALERT DELIVERY" in r.getMessage() for r in caplog.records)


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_healthy_buffered_window_creates_nothing(timescale_db, _fresh_redis):
    """Buffered rows within thresholds drain silently — no retroactive noise."""
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    _ingest(world, "2026-08-25T06:00:00Z", temp_avg=26.0)
    drain_stream("telemetry:historical", count=10)
    assert not _replay_alerts(world).exists()
