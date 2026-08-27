"""M4-05 acceptance — duration gate + escalation sweep + 3-tier delivery.

TASKS acceptance names: test_threshold_breach_no_alert_below_duration,
test_threshold_breach_triggers_alert, plus the tier timeline. Duration-gate
unit tests are pure Redis + injected clock; sweep tests run against real
Postgres via timescale_db (plain functions — fixtures cannot inject into
TestCase methods).
فارسی: پذیرش M4-05 — صدورِ هشدار دقیقاً پس از ۹۰۰ ثانیه، خطِ زمانِ ارتقای
۱۵/۳۰ دقیقه، توقف با کلیدِ بی‌صدا و بستنِ خودکار پس از بهبود.
"""

import json
import logging
import time
import uuid
from datetime import timedelta

import pytest
import redis as redis_lib

from apps.alerts.models import AlertInstance
from apps.alerts.services.breach import consume_fire, track_breach
from apps.alerts.services.redis_keys import (
    active_alert_key,
    breach_key,
    paused_key,
    thresholds_hash,
)
from apps.alerts.services.sweeps import escalate_all
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL  # noqa: E402

SCRATCH_REDIS_URL = "redis://:dev_redis_password@localhost:6379/10"


# ── ① duration gate — pure redis, injected clock ────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    """House standard (ade9e18): NO env mutation. Every service that lazily
    builds a Redis client gets THE SAME integration client injected —
    consumers (drain) AND sweeps (escalate_all) — otherwise whichever module
    imported first freezes the URL and drains silently hit db0 while data
    lives on /11."""
    scratch = redis_lib.Redis.from_url(SCRATCH_REDIS_URL, decode_responses=True)
    scratch.flushdb()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    monkeypatch.setattr("apps.alerts.services.sweeps.system_redis", lambda: client)
    yield client


class TestDurationGate:
    """TUTORIAL §3.3: fire exactly ONCE per episode, exactly AT 900 s."""

    SALOON, METRIC, MAX_T = 7, "temp", 30.0
    T0 = 1_000_000.0

    def _episode(self, client, value=32.0):
        track_breach(client, self.SALOON, self.METRIC, value,
                     self.MAX_T, 0.5, 900, clock_now=self.T0)

    def test_no_fire_below_duration(self, _fresh_redis):
        self._episode(_fresh_redis)
        for probe in (1, 60, 899):
            assert consume_fire(_fresh_redis, self.SALOON, self.METRIC,
                                900, clock_now=self.T0 + probe) is False

    def test_fires_exactly_at_duration(self, _fresh_redis):
        self._episode(_fresh_redis)
        assert consume_fire(_fresh_redis, self.SALOON, self.METRIC,
                            900, clock_now=self.T0 + 900) is True

    def test_never_fires_twice_per_episode(self, _fresh_redis):
        self._episode(_fresh_redis)
        assert consume_fire(_fresh_redis, self.SALOON, self.METRIC,
                            900, clock_now=self.T0 + 900) is True
        assert consume_fire(_fresh_redis, self.SALOON, self.METRIC,
                            900, clock_now=self.T0 + 1200) is False

    def test_fire_mark_preserves_remaining_ttl(self, _fresh_redis):
        self._episode(_fresh_redis)
        _fresh_redis.expire(breach_key(self.SALOON, self.METRIC), 400)
        consume_fire(_fresh_redis, self.SALOON, self.METRIC,
                     900, clock_now=self.T0 + 900)
        ttl = _fresh_redis.ttl(breach_key(self.SALOON, self.METRIC))
        assert 395 <= ttl <= 400


# ── ② escalation sweep — real Postgres ──────────────────────────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_threshold_breach_no_alert_below_duration(timescale_db, _fresh_redis):
    """Full pipeline: one breaching observation must NOT create a row."""
    from apps.telemetry.consumers import drain_stream
    from morghdari.subscriber.pipeline import handle_message

    world = _make_world(_fresh_redis)
    payload = _payload(world["hw"], world["gw"], temp_avg=32.0)
    handle_message(world["redis"], world["topic"], payload,
                   db_fetch=lambda *a: world["slot"].slot_id)
    drain_stream("telemetry:realtime", count=10)

    assert not AlertInstance.objects.filter(org_id=world["org"].org_id).exists()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_threshold_breach_triggers_alert(timescale_db, caplog, _fresh_redis):
    """Sustained breach fires Tier 1; the sweep then walks the tier timeline:
    t+16 min → ACTIVE_TIER_2 (sms_engineer), t+31 min → ACTIVE_TIER_3
    (manager_and_doctor); ack pause freezes; recovery resolves."""
    from apps.telemetry.consumers import drain_stream
    from morghdari.subscriber.pipeline import handle_message

    world = _make_world(_fresh_redis)
    now = time.time()

    # Episode opened 901 s ago → duration met on the next observation.
    world["redis"].set(
        breach_key(world["saloon"].id, "temp"),
        json.dumps({"started_at": now - 901.0, "alerted": False, "value": 31.5}),
        ex=900,
    )
    stream_key = "telemetry:realtime"
    payload = _payload(world["hw"], world["gw"], temp_avg=33.0)
    ret = handle_message(world["redis"], world["topic"], payload,
                         db_fetch=lambda *a: world["slot"].slot_id)
    n_drained = drain_stream(stream_key, count=10)
    dlq_keys = [k for k in world["redis"].keys("*dlq*") if world["redis"].type(k) == "stream"]
    dlq_len = sum(world["redis"].xlen(k) for k in dlq_keys)

    alert = AlertInstance.objects.get(org_id=world["org"].org_id)
    assert alert.current_status == "ACTIVE_TIER_1"
    assert float(alert.current_value) == 33.0

    # ── Tier 2 at 15 min ────────────────────────────────────────────────────
    age(alert, minutes=16)
    with caplog.at_level(logging.INFO, logger="apps.alerts.services.sweeps"):
        escalate_all()
    alert.refresh_from_db()
    assert alert.current_status == "ACTIVE_TIER_2"
    assert alert.highest_tier_reached == 2
    assert any("sms_engineer" in r.getMessage() for r in caplog.records)

    # Sweep is idempotent: second pass promotes nothing.
    assert escalate_all() == 0
    alert.refresh_from_db()
    assert alert.current_status == "ACTIVE_TIER_2"

    # ── Ack pause freezes the timer even past Tier 3 threshold ─────────────
    _fresh_redis.set(paused_key(str(alert.id)), "1", ex=600)
    age(alert, minutes=40)
    escalate_all()
    alert.refresh_from_db()
    assert alert.current_status == "ACTIVE_TIER_2"
    _fresh_redis.delete(paused_key(str(alert.id)))

    # ── Tier 3 at 30 min once unpaused ──────────────────────────────────────
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="apps.alerts.services.sweeps"):
        escalate_all()
    alert.refresh_from_db()
    assert alert.current_status == "ACTIVE_TIER_3"
    assert alert.highest_tier_reached == 3
    assert any("manager_and_doctor" in r.getMessage() for r in caplog.records)

    # ── Recovery: breach key gone ⇒ RESOLVED + active pointer cleared ───────
    _fresh_redis.delete(breach_key(world["saloon"].id, "temp"))
    escalate_all()
    alert.refresh_from_db()
    assert alert.current_status == "RESOLVED"
    assert alert.resolved_at is not None
    assert not _fresh_redis.exists(
        active_alert_key(world["saloon"].id, "temp")
    )


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_replay_rows_are_never_touched_by_sweep(timescale_db, _fresh_redis):
    """REPLAY_ANALYSIS rows stay historical: no promotion, no delivery."""
    world = _make_world(_fresh_redis)
    alert = AlertInstance.objects.create(
        org=world["org"], farm=world["saloon"].farm, saloon=world["saloon"],
        metric_type="temp", current_status="ACTIVE_TIER_1",
        current_value=32.00, source="REPLAY_ANALYSIS",
    )
    _fresh_redis.set(
        breach_key(world["saloon"].id, "temp"),
        json.dumps({"started_at": time.time() - 3600.0,
                    "alerted": True, "value": 32.0}),
        ex=900,
    )
    age(alert, minutes=45)
    assert escalate_all() == 0
    alert.refresh_from_db()
    assert alert.current_status == "ACTIVE_TIER_1"


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_world(redis_client):
    tag = "e5" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"f-org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot, org=org
    )
    rc = redis_client
    rc.hset(thresholds_hash(saloon.id), mapping={
        "temp_max": "30.00", "temp_min": "18.00",
        "nh3_enabled": "1", "operational_state": "ACTIVE_FLOCK",
    })
    return {
        "org": org, "saloon": saloon, "slot": slot, "redis": rc,
        "topic": f"{org.org_id}/farms/1/gateways/GW-{tag}/telemetry",
        "hw": f"hw-{tag}", "gw": f"GW-{tag}",
    }


def _payload(hw, gw, temp_avg):
    return json.dumps({
        "gateway_hardware_id": hw, "gateway_slot_id": gw,
        "schema_version": "2.0", "device_ts": "2026-08-26T12:00:00Z",
        "is_buffered": False,
        "readings": [{"pack_id": "P-A",
                      "metrics": {"temp": {"avg": temp_avg}}}],
    }).encode()


def age(alert, minutes):
    """Backdate triggered_at so the sweep's clock math sees an aged alert."""
    AlertInstance.objects.filter(pk=alert.pk).update(
        triggered_at=alert.triggered_at - timedelta(minutes=minutes),
    )
    alert.refresh_from_db()
