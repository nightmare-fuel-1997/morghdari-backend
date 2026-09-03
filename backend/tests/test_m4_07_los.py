"""M4-07 acceptance — LOS watchdog: 60 s sweep, 300 s grace, exactly-once.

TASKS acceptance names: test_los_watchdog_sweep_fires_after_ttl + suppression
tests green. The 300 s grace IS the sensor:status TTL written by the consumer
on every live reading; expiry feeds the sweep. Plain-function PG modules
(memory gotcha: pytest fixtures cannot inject into TestCase methods);
timescale_db replays the hypertable DDL that --nomigrations hides. House
standard ade9e18: NO env mutation — the autouse fixture injects THE integration
Redis client into BOTH consumers._redis_client (drain) and
alerts.services.sweeps.system_redis (watchdog).

فارسی: پذیرش M4-07 — قطعِ دادهٔ حسگر پس از ۳۰۰ ثانیه دقیقاً یک هشدار می‌سازد،
پس از ری‌استارتِ ردیس سگمنت کاملاً ساکت است و بازگشتِ داده هشدار را می‌بندد.
"""

import json
import logging
import uuid

import pytest
import redis as redis_lib

from apps.alerts.models import AlertInstance
from apps.alerts.services.redis_keys import (
    los_fired_key,
    sensor_status_key,
    thresholds_hash,
)
from apps.alerts.services.sweeps import escalate_all, los_watchdog
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.system_config.models import SystemConfiguration
from apps.tenants.models import Organization
from morghdari.subscriber.pipeline import handle_message

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL, redis_base_url  # noqa: E402

# Dedicated scratch logical DB for this module (never the demo/dev db0,
# never the integration /11): wiped per test by the autouse fixture.
SCRATCH_REDIS_URL = redis_base_url() + "/13"


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    scratch = redis_lib.Redis.from_url(SCRATCH_REDIS_URL, decode_responses=True)
    scratch.flushdb()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    monkeypatch.setattr("apps.alerts.services.sweeps.system_redis", lambda: client)
    # Default: a MATURE Redis (24 h uptime). The dev/CI container restarts
    # freely and its real uptime would trip the DR-T3 suppression check,
    # making every firing test flaky. Suppression tests override with _uptime().
    monkeypatch.setattr(
        client, "info", lambda section=None: {"uptime_in_seconds": 86_400}
    )
    yield client


def _uptime(client, seconds, monkeypatch):
    """Fake a Redis server uptime for the post-restart suppression check."""
    monkeypatch.setattr(
        client, "info", lambda section=None: {"uptime_in_seconds": seconds}
    )


def _make_world():
    tag = "e7" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot_a = SaloonSensorSlot.objects.create(
        slot_id=f"a-{tag}", saloon=saloon, org=org, position=position
    )
    slot_b = SaloonSensorSlot.objects.create(
        slot_id=f"b-{tag}", saloon=saloon, org=org, position=position
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot_a, org=org
    )
    return {
        "org": org, "farm": farm, "saloon": saloon,
        "slot_a": slot_a, "slot_b": slot_b,
        "redis": redis_lib.Redis.from_url(REDIS_URL, decode_responses=True),
        "topic": f"{org.org_id}/farms/1/gateways/GW-{tag}/telemetry",
        "hw": f"hw-{tag}", "gw": f"GW-{tag}",
    }


def _mark_online(rc, saloon, slot, ttl=300):
    rc.set(sensor_status_key(saloon.id, slot.pk), "ONLINE", ex=ttl)


def _los_alerts(saloon):
    return AlertInstance.objects.filter(saloon_id=saloon.id, metric_type="los")


# ── ① the named acceptance ───────────────────────────────────────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_los_watchdog_sweep_fires_after_ttl(timescale_db, _fresh_redis):
    """A provisioned, ACTIVE slot whose sensor:status key has expired (no
    reading for the 300 s TTL) triggers EXACTLY ONE LOS alert routed to
    installer + hardware team; the fresh slot in the same saloon does not
    block it and the saloon-scoped row is not duplicated (Testing §2.2)."""
    world = _make_world()
    rc = _fresh_redis
    _mark_online(rc, world["saloon"], world["slot_a"])  # fresh — no alert from it
    # slot_b never checked in → its key expired ⇒ saloon is in LOS.

    fired = los_watchdog()
    assert fired == 1
    alerts = _los_alerts(world["saloon"])
    assert alerts.count() == 1
    alert = alerts.get()
    assert alert.current_status == "ACTIVE_TIER_1"
    assert alert.source == "REALTIME"
    assert alert.current_value is None
    assert alert.resolved_at is None
    # The NX guard is armed for every missing slot of the episode.
    assert rc.exists(los_fired_key(world["saloon"].id, world["slot_b"].pk))


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_los_alert_fires_exactly_once_across_sweeps(timescale_db, _fresh_redis):
    """The 60 s cadence re-runs while the sensor stays down: dedup (NX marker
    + unresolved-row check) must keep it at exactly ONE row."""
    world = _make_world()
    for _ in range(3):
        los_watchdog()
    assert _los_alerts(world["saloon"]).count() == 1


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_no_alert_while_status_keys_fresh(timescale_db, _fresh_redis):
    world = _make_world()
    rc = _fresh_redis
    _mark_online(rc, world["saloon"], world["slot_a"])
    _mark_online(rc, world["saloon"], world["slot_b"])

    assert los_watchdog() == 0
    assert not _los_alerts(world["saloon"]).exists()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_inactive_slots_are_never_watched(timescale_db, _fresh_redis):
    """'Provisioned, ACTIVE slot' — a decommissioned slot cannot page."""
    world = _make_world()
    SaloonSensorSlot.objects.filter(pk=world["slot_a"].pk).update(is_active=False)
    SaloonSensorSlot.objects.filter(pk=world["slot_b"].pk).update(is_active=False)

    assert los_watchdog() == 0
    assert not _los_alerts(world["saloon"]).exists()


# ── ② recovery — owned by the watchdog, NOT by the escalation sweep ─────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_los_recovery_resolves_and_rearms(timescale_db, _fresh_redis):
    """All slots reporting again ⇒ open LOS row resolves + markers clear;
    a NEW outage afterwards pages again (one alert per EPISODE, not per life)."""
    world = _make_world()
    rc = _fresh_redis
    los_watchdog()
    alert = _los_alerts(world["saloon"]).get()

    _mark_online(rc, world["saloon"], world["slot_a"])
    _mark_online(rc, world["saloon"], world["slot_b"])
    assert los_watchdog() == 0
    alert.refresh_from_db()
    assert alert.current_status == "RESOLVED"
    assert alert.resolved_at is not None
    assert not rc.exists(los_fired_key(world["saloon"].id, world["slot_b"].pk))

    # Next outage: the episode re-arms cleanly.
    rc.delete(
        sensor_status_key(world["saloon"].id, world["slot_a"].pk),
        sensor_status_key(world["saloon"].id, world["slot_b"].pk),
    )
    assert los_watchdog() == 1
    assert _los_alerts(world["saloon"]).filter(resolved_at__isnull=True).count() == 1


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_escalation_sweep_leaves_los_rows_alone(timescale_db, _fresh_redis):
    """LOS rows have no breach key — escalate_all must neither resolve nor
    promote them; the watchdog owns the whole lifecycle (M4-07 exclusion)."""
    world = _make_world()
    los_watchdog()
    alert = _los_alerts(world["saloon"]).get()

    escalate_all()
    alert.refresh_from_db()
    assert alert.current_status == "ACTIVE_TIER_1"
    assert alert.resolved_at is None
    assert alert.highest_tier_reached == 1


# ── ③ Redis-restart suppression (DR T3) — fully config-driven ────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_los_suppressed_after_redis_restart(timescale_db, _fresh_redis, monkeypatch):
    """After a Redis restart every sensor:status key is gone at once — the
    sweep must stay silent for los_suppress_after_redis_restart_min (DR T3)."""
    world = _make_world()
    _uptime(_fresh_redis, 60, monkeypatch)  # restarted 1 min ago
    assert los_watchdog() == 0
    assert not _los_alerts(world["saloon"]).exists()

    # Just past the 10-minute window: the same outage pages normally.
    _uptime(_fresh_redis, 601, monkeypatch)
    assert los_watchdog() == 1


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_suppression_window_follows_config(timescale_db, _fresh_redis, monkeypatch):
    """D21: the window is data, not code — raising the seeded key widens the
    silent window without a redeploy."""
    world = _make_world()
    SystemConfiguration.objects.filter(
        key="los_suppress_after_redis_restart_min"
    ).update(value="120")

    _uptime(_fresh_redis, 3600, monkeypatch)  # 1 h uptime < 120 min window
    assert los_watchdog() == 0
    assert not _los_alerts(world["saloon"]).exists()

    _uptime(_fresh_redis, 7300, monkeypatch)  # past 120 min → fires
    assert los_watchdog() == 1


# ── ④ the 300 s grace is written by the consumer (pipeline step 5) ──────────

def _ingest(world, device_ts: str, temp_avg: float, buffered: bool = False):
    payload = json.dumps({
        "gateway_hardware_id": world["hw"],
        "gateway_slot_id": world["gw"],
        "schema_version": "2.0",
        "device_ts": device_ts,
        "is_buffered": buffered,
        "readings": [{"pack_id": "P-A",
                      "metrics": {"temp": {"avg": temp_avg}}}],
    }).encode()
    return handle_message(world["redis"], world["topic"], payload,
                          db_fetch=lambda *a: world["slot_a"].slot_id)


def _warm_thresholds(rc, saloon):
    rc.hset(thresholds_hash(saloon.id), mapping={
        "temp_max": "30.00", "temp_min": "18.00",
        "nh3_enabled": "1", "operational_state": "ACTIVE_FLOCK",
    })


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_live_reading_refreshes_sensor_status(timescale_db, _fresh_redis):
    """Every live reading refreshes sensor:status with the seeded TTL — its
    expiry is the watchdog's 300 s grace (Walkthrough Part 3, step 5)."""
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    rc = world["redis"]
    _warm_thresholds(rc, world["saloon"])

    _ingest(world, "2026-08-26T12:00:00Z", temp_avg=26.0)
    drain_stream("telemetry:realtime", count=10)

    key = sensor_status_key(world["saloon"].id, world["slot_a"].pk)
    assert rc.exists(key)
    ttl = rc.ttl(key)
    assert 0 < ttl <= 300


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_buffered_reading_does_not_refresh_sensor_status(timescale_db, _fresh_redis):
    """A replayed reading carries an OLD device_ts — it proves history, not
    liveness; a replay storm must never mask a real outage."""
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    rc = world["redis"]
    _warm_thresholds(rc, world["saloon"])
    key = sensor_status_key(world["saloon"].id, world["slot_a"].pk)
    rc.set(key, "ONLINE", ex=100)

    _ingest(world, "2026-08-25T06:00:00Z", temp_avg=26.0, buffered=True)
    drain_stream("telemetry:historical", count=10)

    assert rc.ttl(key) == 100  # untouched by the buffered entry


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_los_log_routes_to_installer_and_hardware_team(timescale_db, _fresh_redis, caplog):
    """Delivery is a stub until v0.6, but the ROUTING decision is observable
    now: the LOS warning names installer + hardware team (Testing §2.2)."""
    world = _make_world()
    with caplog.at_level(logging.WARNING, logger="apps.alerts.services.sweeps"):
        los_watchdog()
    assert any(
        "installer+hardware_team" in r.getMessage() for r in caplog.records
    )