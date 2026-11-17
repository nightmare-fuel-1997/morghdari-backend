"""Auto-RoC 10s Live Telemetry Fast-Path & Single-Slot Velocity Guard tests.

Covers:
1. T10 topic parsing (`parse_live_topic`, `is_live_telemetry_topic`).
2. `handle_live_message` fast-path (Gate 1/2/2.5/3/5 validation, Redis
   `live_stream_active_key` state tracking, direct WebSocket `telemetry.live`
   push without touching Celery or TimescaleDB).
3. `SaloonTelemetryConsumer.telemetry_live` and `FarmTelemetryConsumer.telemetry_live`
   (Fix 2: Flutter compatibility).
4. `velocity.py` Single-Slot Auto-RoC Grace & Plateau Protection (Fix 1):
   - Single-slot spike with active live stream holds Tier-1 for 30s and
     PRESERVES the pre-spike baseline in `last_key`.
   - Plateau Trap protection: if a real localized fire plateaus at 36.5°C and
     its 10s RoC enters SETTLING/ENDED while still breaching the preserved
     pre-spike baseline, Tier-1 fires!
   - Genuine manual sensor test: if the reading settles AND returns toward
     the pre-spike baseline (or settles before any 60s grace hold), it is
     logged as `TEST_SPIKE_IGNORED` and suppressed.
   - Multi-slot simultaneous spikes (>= 2 slots in the same saloon) ALWAYS
     fire Tier-1 immediately with zero delay.

فارسی: تست‌های مسیر سریع تله‌متری زنده (Auto-RoC 10s) و محافظت در برابر هشدار
کاذب تک‌سنسوره و تلهٔ فلات آتش‌سوزی در velocity.py.
"""

import json
import pytest
import redis as redis_lib

from apps.alerts.services.redis_keys import (
    live_stream_active_key,
    single_slot_spike_grace_key,
    slot_last_metrics_key,
    slot_saloon_cache_key,
)
from apps.alerts.services.velocity import check_velocity_emergency
from apps.telemetry import ws
from morghdari.subscriber import main as sub
from morghdari.subscriber.topics import (
    TopicParseError,
    is_live_telemetry_topic,
    parse_live_topic,
)
from tests.conftest import redis_base_url

REDIS_URL = redis_base_url() + "/10"
SALOON = 42
ORG_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def redis_client():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client


def _thresholds(**overrides):
    base = {
        "temp_max": "30.00",
        "temp_min": "18.00",
        "nh3_enabled": "1",
        "operational_state": "ACTIVE_FLOCK",
    }
    base.update(overrides)
    return base


# ── 1. Topic parsing ─────────────────────────────────────────────────────────

def test_live_topic_regex_matches_t10_and_rejects_t1():
    t10 = f"{ORG_ID}/farms/12/gateways/FARM-01-GW-A/telemetry/live"
    t1 = f"{ORG_ID}/farms/12/gateways/FARM-01-GW-A/telemetry"
    assert is_live_telemetry_topic(t10) is True
    assert is_live_telemetry_topic(t1) is False
    assert parse_live_topic(t10) == (ORG_ID, "12", "FARM-01-GW-A")

    with pytest.raises(TopicParseError):
        parse_live_topic(t1)


# ── 2. Subscriber handle_live_message fast-path ──────────────────────────────

def test_handle_live_message_pushes_to_ws_and_sets_redis_marker(redis_client):
    topic = f"{ORG_ID}/farms/12/gateways/GW-01/telemetry/live"
    payload = json.dumps({
        "gateway_hardware_id": "AA:BB:CC:DD:EE:FF",
        "gateway_slot_id": "GW-01",
        "schema_version": "2.0",
        "device_ts": "2026-11-17T14:00:10Z",
        "stream_state": "STARTED",
        "session_id": "GW-01-P-A-1000",
        "readings": [
            {
                "pack_id": "P-A",
                "trigger_metric": "temp",
                "roc_delta": 1.4,
                "roc_window_sec": 10,
                "metrics": {"temp": 31.2, "humidity": 55.0},
                "sensor_errors": {},
            }
        ],
    }).encode("utf-8")

    pushed = []

    res = sub.handle_live_message(
        redis_client,
        topic,
        payload,
        db_fetch=lambda org, hw, pack: "S01-PACK-A",
        meta_fetch=lambda org, farm, gw: {"farm_id": 12, "provisioning_status": "PROVISIONED"},
        saloon_fetch=lambda org, slot: SALOON,
        ws_pusher=lambda org, items: pushed.append((org, items)),
    )

    assert res is None
    assert len(pushed) == 1
    org, items = pushed[0]
    assert org == ORG_ID
    assert len(items) == 1
    assert items[0]["saloon_id"] == SALOON
    assert items[0]["slot_id"] == "S01-PACK-A"
    assert items[0]["stream_state"] == "STARTED"
    assert items[0]["trigger_metric"] == "temp"
    assert items[0]["roc_delta"] == 1.4
    assert items[0]["metrics"] == {"temp": 31.2, "humidity": 55.0}

    # Verify Redis live marker was set for velocity.py
    raw_marker = redis_client.get(live_stream_active_key(SALOON, "S01-PACK-A"))
    assert raw_marker is not None
    marker = json.loads(raw_marker)
    assert marker["stream_state"] == "STARTED"
    assert marker["trigger_metric"] == "temp"


def test_handle_live_message_rejects_identity_mismatch_to_dlq(redis_client):
    topic = f"{ORG_ID}/farms/12/gateways/GW-01/telemetry/live"
    payload = json.dumps({
        "gateway_hardware_id": "AA:BB",
        "gateway_slot_id": "GW-SPOOFED",
        "schema_version": "2.0",
        "device_ts": "2026-11-17T14:00:10Z",
        "stream_state": "ACTIVE",
        "readings": [],
    }).encode("utf-8")

    dlq_id = sub.handle_live_message(redis_client, topic, payload)
    assert dlq_id is not None


def test_on_message_routes_live_telemetry_to_fast_path(monkeypatch, redis_client):
    called = []
    monkeypatch.setattr(sub, "_redis", lambda: redis_client)
    monkeypatch.setattr(
        sub,
        "handle_live_message",
        lambda r, t, p, **kw: called.append((t, p)),
    )

    class _Msg:
        topic = f"{ORG_ID}/farms/12/gateways/GW-01/telemetry/live"
        payload = b"{}"
        qos = 0
        mid = 1

    sub._on_message(None, None, _Msg())
    assert len(called) == 1
    assert called[0][0].endswith("/telemetry/live")


# ── 3. WebSocket message builder & consumer handlers (incl. Fix 2) ───────────

def test_telemetry_live_message_shape_and_consumer_forwarding():
    msg = ws.telemetry_live_message(
        saloon_id=SALOON,
        slot_id="S01-PACK-A",
        device_ts="2026-11-17T14:00:10Z",
        stream_state="ACTIVE",
        session_id="sess-1",
        trigger_metric="temp",
        roc_delta=1.1,
        roc_window_sec=10,
        metrics={"temp": 30.5, "humidity": 60.0},
    )
    assert msg["type"] == "telemetry.live"
    assert msg["stream_state"] == "ACTIVE"
    assert msg["metrics"]["temp"] == 30.5

    # Core SaloonTelemetryConsumer handler
    from apps.telemetry.ws_consumer import SaloonTelemetryConsumer

    consumer = SaloonTelemetryConsumer()
    sent_core = []
    consumer.send_json = sent_core.append
    consumer.telemetry_live(msg)
    assert sent_core == [msg]

    # Fix 2: Flutter compat FarmTelemetryConsumer handler
    from apps.compat.consumers_compat import FarmTelemetryConsumer

    compat_consumer = FarmTelemetryConsumer()
    compat_consumer.saloon_slugs = {SALOON: "saloon-42-slug"}
    sent_compat = []
    compat_consumer.send_json = sent_compat.append
    compat_consumer.telemetry_live(msg)
    assert len(sent_compat) == 1
    flat = sent_compat[0]
    assert flat["device_slug"] == "saloon-42-slug"
    assert flat["temperature"] == "30.5"
    assert flat["humidity"] == "60.0"
    assert flat["stream_state"] == "ACTIVE"
    assert flat["trigger_metric"] == "temp"


# ── 4. Velocity Single-Slot Grace & Fix 1 Plateau Protection ─────────────────

class TestVelocitySingleSlotGraceAndPlateauFix:
    def test_single_slot_spike_holds_in_grace_and_preserves_baseline(self, redis_client):
        """Fix 1: When S01-PACK-A spikes from 28.0°C to 36.0°C while its live
        stream is ACTIVE, Tier-1 is held for 30s AND `last_key['temp_avg']`
        MUST stay '28.0' (never overwritten with '36.0')."""
        slot = "S01-PACK-A"
        last_key = slot_last_metrics_key(SALOON, slot)
        redis_client.hset(last_key, mapping={"temp_avg": "28.0", "ts": "940.0"})
        redis_client.set(
            live_stream_active_key(SALOON, slot),
            json.dumps({"stream_state": "ACTIVE", "trigger_metric": "temp"}),
            ex=60,
        )

        # At t=1000s, 60s window reports 36.0°C (+8.0°C >= 5.0°C threshold)
        res = check_velocity_emergency(
            redis_client,
            SALOON,
            _thresholds(),
            {"temp": {"avg": 36.0}},
            clock_now=1000.0,
            slot_id=slot,
        )
        assert res is None  # Suppressed during 30s grace hold

        # CRITICAL (Fix 1): pre-spike baseline 28.0 MUST be preserved!
        stored = redis_client.hgetall(last_key)
        assert float(stored["temp_avg"]) == 28.0
        assert redis_client.get(single_slot_spike_grace_key(SALOON, slot, "temp")) is not None

    def test_plateau_trap_fires_tier1_when_sustained_high_even_if_live_settled(self, redis_client):
        """Fix 1 Plateau Trap: A localized fire jumps 28°C -> 36°C (held in grace
        at t=1000s), then plateaus at 36.5°C at t=1060s. Because it plateaued,
        the Gateway's 10s RoC dropped to 0 and emitted stream_state='ENDED'.
        Because the reading (36.5°C) is STILL breaching against the preserved
        28.0°C baseline, `velocity.py` MUST NOT ignore it as a test — it MUST
        fire Tier-1!"""
        slot = "S01-PACK-A"
        last_key = slot_last_metrics_key(SALOON, slot)
        redis_client.hset(last_key, mapping={"temp_avg": "28.0", "ts": "940.0"})

        # Cycle 1 (t=1000s): live stream is ACTIVE, temp spikes to 36.0°C -> grace hold
        redis_client.set(
            live_stream_active_key(SALOON, slot),
            json.dumps({"stream_state": "ACTIVE", "trigger_metric": "temp"}),
            ex=60,
        )
        res1 = check_velocity_emergency(
            redis_client,
            SALOON,
            _thresholds(),
            {"temp": {"avg": 36.0}},
            clock_now=1000.0,
            slot_id=slot,
        )
        assert res1 is None

        # Cycle 2 (t=1060s): fire plateaus at 36.5°C, live stream settled to ENDED
        redis_client.set(
            live_stream_active_key(SALOON, slot),
            json.dumps({"stream_state": "ENDED", "trigger_metric": "temp"}),
            ex=60,
        )
        res2 = check_velocity_emergency(
            redis_client,
            SALOON,
            _thresholds(),
            {"temp": {"avg": 36.5}},
            clock_now=1060.0,
            slot_id=slot,
        )
        assert res2 is not None, "Plateaued high temperature after grace hold MUST fire Tier-1!"
        assert res2["metric"] == "temp"
        assert res2["tier"] == 1
        assert res2["value"] == 36.5

    def test_genuine_lighter_test_that_recovers_toward_baseline_is_ignored(self, redis_client, caplog):
        """When a brief lighter test spikes at t=1000s (held in grace) and then
        cools back to 29.5°C at t=1060s (delta +1.5°C < 5.0°C against the
        preserved 28.0°C baseline), it is logged as TEST_SPIKE_IGNORED and
        never fires Tier-1."""
        slot = "S01-PACK-A"
        last_key = slot_last_metrics_key(SALOON, slot)
        redis_client.hset(last_key, mapping={"temp_avg": "28.0", "ts": "940.0"})
        redis_client.set(
            live_stream_active_key(SALOON, slot),
            json.dumps({"stream_state": "ACTIVE", "trigger_metric": "temp"}),
            ex=60,
        )

        # Cycle 1: spike held in grace
        assert check_velocity_emergency(
            redis_client, SALOON, _thresholds(), {"temp": {"avg": 35.0}},
            clock_now=1000.0, slot_id=slot,
        ) is None

        # Cycle 2: sensor cooled back to 29.5°C (within +1.5°C of 28.0°C baseline)
        redis_client.set(
            live_stream_active_key(SALOON, slot),
            json.dumps({"stream_state": "ENDED", "trigger_metric": "temp"}),
            ex=60,
        )
        with caplog.at_level("INFO"):
            res2 = check_velocity_emergency(
                redis_client, SALOON, _thresholds(), {"temp": {"avg": 29.5}},
                clock_now=1060.0, slot_id=slot,
            )
        assert res2 is None
        assert "TEST_SPIKE_IGNORED" in caplog.text
        assert redis_client.get(single_slot_spike_grace_key(SALOON, slot, "temp")) is None
        assert float(redis_client.hgetall(last_key)["temp_avg"]) == 29.5

    def test_multi_slot_simultaneous_spikes_fire_immediately_without_grace_delay(self, redis_client):
        """When Slot A and Slot B in the same saloon both spike within 90s,
        there is NO grace delay even if live streams are active."""
        slot_a = "S01-PACK-A"
        slot_b = "S01-PACK-B"
        for s in (slot_a, slot_b):
            redis_client.hset(
                slot_last_metrics_key(SALOON, s),
                mapping={"temp_avg": "26.0", "ts": "940.0"},
            )
            redis_client.set(
                live_stream_active_key(SALOON, s),
                json.dumps({"stream_state": "STARTED", "trigger_metric": "temp"}),
                ex=60,
            )

        # Slot A spikes first -> single slot so far, held in grace
        res_a = check_velocity_emergency(
            redis_client, SALOON, _thresholds(), {"temp": {"avg": 33.0}},
            clock_now=1000.0, slot_id=slot_a,
        )
        assert res_a is None

        # Slot B spikes 2 seconds later -> 2 slots spiking in same saloon -> FIRES IMMEDIATELY!
        res_b = check_velocity_emergency(
            redis_client, SALOON, _thresholds(), {"temp": {"avg": 32.5}},
            clock_now=1002.0, slot_id=slot_b,
        )
        assert res_b is not None
        assert res_b["metric"] == "temp"
        assert res_b["tier"] == 1
