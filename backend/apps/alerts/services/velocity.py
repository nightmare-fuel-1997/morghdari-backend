"""Velocity emergencies — Decision 10: evaluated BEFORE any suppression.

Two lanes, checked in order per enriched reading:
  1. NH₃ max_ts spike: any metric whose reading carries max_ts ≥
     nh3_max_ts_velocity_threshold fires INSTANT Tier 1. This means the sensor
     caught a SPIKE mid-window, not a slow drift. Bypasses grace keys, mutes,
     operational state — every suppression gate.
  2. Temperature rate-of-change: current avg vs cached last avg must differ by
     ≥ temp_velocity_delta_60s °C within one window.

Single-Slot Auto-RoC Test Guard & Plateau Protection:
  When a velocity spike originates from ONLY ONE sensor slot in a saloon while
  an Auto-RoC live stream (`sensor:live_stream:{saloon}:{slot}`) is active:
    - Multi-slot simultaneous spikes (>= 2 slots in the saloon within 90s)
      ALWAYS fire Tier-1 immediately (real saloon-wide emergency).
    - While held in the 30s single-slot grace window (`STARTED`/`ACTIVE`), the
      pre-spike baseline in `last_key` is PRESERVED (never overwritten with the
      spiked value, only its TTL is refreshed).
    - If a spike was previously held in grace and the live stream reaches
      `SETTLING`/`ENDED`, it is ONLY ignored (`TEST_SPIKE_IGNORED`) if the
      reading is genuinely recovering toward the pre-spike baseline (i.e. no
      longer breaching the velocity delta against the preserved baseline). A
      real localized fire that plateaus at a high temperature still breaches
      against the preserved baseline and fires Tier-1!

Both lanes gated by `{metric}_enabled` flags from the threshold hash.
NH₃ invariant: ALWAYS evaluates `.max`, never `.avg` (Saas.md mandate).
فارسی: بررسیِ سرعت‌های خطرناک قبل از همهٔ فیلترها؛ آمونیاک همیشه با مقدارِ بیشینه
سنجیده می‌شود و تغییرِ ناگهانیِ دما هم فوراً هشدار می‌دهد (همراه با گاردِ تستِ تک‌سنسوره
و حفظِ خطِ مبنا در برابر تلهٔ فلاتِ آتش‌سوزی).
"""

import json
import logging
import time as _time

from apps.alerts.services.redis_keys import (
    live_stream_active_key,
    single_slot_spike_grace_key,
    slot_last_metrics_key,
)

logger = logging.getLogger(__name__)

NH3_MAX_TS_THRESHOLD_DEFAULT = 50
TEMP_RATE_DELTA_DEFAULT = 5.0
SINGLE_SLOT_GRACE_SEC_DEFAULT = 30


def _config_int(config_getter, key: str, default: int) -> int:
    raw = config_getter(key, default)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _config_float(config_getter, key: str, default: float) -> float:
    raw = config_getter(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _peer_spikes_key(saloon_id: int) -> str:
    return f"saloon:{saloon_id}:peer_velocity_spikes"


def _should_suppress_single_slot_test_spike(
    redis_client,
    saloon_id: int,
    slot_id: str | None,
    metric: str,
    now: float,
    grace_sec: int,
    *,
    still_breaching_baseline: bool = True,
) -> tuple[bool, bool]:
    """Evaluate single-slot Auto-RoC test suppression.

    Returns `(suppress, preserve_baseline)`:
      - `suppress`: True if Tier-1 firing should be suppressed/delayed this cycle.
      - `preserve_baseline`: True if the caller MUST NOT overwrite `last_key`
        with the spiked reading (keeps the pre-spike baseline alive during a
        30s grace hold so a plateaued fire still breaches on the next cycle).
    """
    if not slot_id:
        return False, False

    # 1. Record this slot's spike in the saloon-wide peer tracker (TTL 90s)
    peers_key = _peer_spikes_key(saloon_id)
    pipe = redis_client.pipeline()
    pipe.hset(peers_key, slot_id, str(now))
    pipe.expire(peers_key, 90)
    pipe.execute()

    all_spikes = redis_client.hgetall(peers_key) or {}
    active_spiking_slots = [
        s for s, ts_str in all_spikes.items()
        if (now - float(ts_str)) <= 90.0
    ]
    if len(active_spiking_slots) > 1:
        # Multi-slot simultaneous spike -> REAL farm emergency, NEVER delay!
        return False, False

    # 2. Check if an Auto-RoC live stream is active for this single slot
    raw_live = redis_client.get(live_stream_active_key(saloon_id, slot_id))
    if not raw_live:
        return False, False

    try:
        live_info = json.loads(raw_live)
    except ValueError:
        return False, False

    stream_state = str(live_info.get("stream_state") or "").upper()
    grace_key = single_slot_spike_grace_key(saloon_id, slot_id, metric)
    first_seen_str = redis_client.get(grace_key)

    # 3. Live stream is SETTLING or ENDED
    if stream_state in {"SETTLING", "ENDED"}:
        if first_seen_str is not None and still_breaching_baseline:
            # PLATEAU TRAP GUARD (Fix 1): This slot was previously held in
            # grace, and even though its 10s RoC settled, the 60s reading is
            # STILL breaching the velocity threshold against the preserved
            # pre-spike baseline (sustained high plateau = real fire, not a
            # brief lighter test that cooled back down). Fire Tier-1 now!
            redis_client.delete(grace_key)
            logger.warning(
                "VELOCITY_PLATEAU_FIRE: saloon=%s slot=%s metric=%s settled at "
                "sustained high level against pre-spike baseline — firing Tier-1",
                saloon_id, slot_id, metric,
            )
            return False, False

        redis_client.delete(grace_key)
        logger.info(
            "TEST_SPIKE_IGNORED: saloon=%s slot=%s metric=%s (live stream %s)",
            saloon_id, slot_id, metric, stream_state,
        )
        return True, False

    # 4. Stream is STARTED or ACTIVE -> enforce the 30s grace window and
    #    PRESERVE the pre-spike baseline in last_key!
    if first_seen_str is None:
        redis_client.set(grace_key, str(now), ex=grace_sec * 4)
        logger.info(
            "VELOCITY_SINGLE_SLOT_GRACE_STARTED: delaying Tier-1 for %ds "
            "(saloon=%s slot=%s metric=%s stream_state=%s)",
            grace_sec, saloon_id, slot_id, metric, stream_state,
        )
        return True, True

    elapsed = now - float(first_seen_str)
    if elapsed < grace_sec:
        logger.debug(
            "VELOCITY_SINGLE_SLOT_GRACE_HOLD: %.1fs/%ds elapsed for saloon=%s slot=%s",
            elapsed, grace_sec, saloon_id, slot_id,
        )
        return True, True

    # Grace window expired and spike still hasn't settled -> fire Tier-1!
    redis_client.delete(grace_key)
    return False, False


def check_velocity_emergency(
    redis_client,
    saloon_id: int,
    thresholds: dict,
    metrics: dict,
    clock_now: float | None = None,
    config_getter=None,
    slot_id: str | None = None,
) -> dict | None:
    """Evaluate BOTH velocity lanes against the enriched reading's metrics.

    Returns an alert-dict when an emergency fires, or None when all values
    are within normal operational bounds (or suppressed as a single-slot test).
    Never raises.
    """
    config = config_getter or _noop_config
    now = _time.time() if clock_now is None else clock_now
    grace_sec = _config_int(
        config, "single_slot_spike_grace_sec", SINGLE_SLOT_GRACE_SEC_DEFAULT
    )

    # ── Lane 1: NH₃ max_ts spike (Decision 10: runs FIRST, before ALL suppression)
    nh3_enabled = thresholds.get("nh3_enabled") == "1"
    nh3_data = metrics.get("nh3")
    if nh3_enabled and isinstance(nh3_data, dict):
        max_ts = nh3_data.get("max_ts")
        spike_threshold = _config_int(
            config, "nh3_max_ts_velocity_threshold", NH3_MAX_TS_THRESHOLD_DEFAULT
        )
        if max_ts is not None and int(max_ts) >= spike_threshold:
            suppress, _ = _should_suppress_single_slot_test_spike(
                redis_client,
                saloon_id,
                slot_id,
                "nh3",
                now,
                grace_sec,
                still_breaching_baseline=True,
            )
            if not suppress:
                max_val = nh3_data.get("max")
                return {
                    "metric": "nh3",
                    "value": max_val,
                    "tier": 1,
                    "reason": (
                        f"NH3 velocity emergency: max_ts={max_ts} >= {spike_threshold}"
                    ),
                }

    # ── Lane 2: temperature rate-of-change (Walkthrough line ~700)
    temp_data = metrics.get("temp")
    if isinstance(temp_data, dict) and "avg" in temp_data:
        last_key = (
            slot_last_metrics_key(saloon_id, slot_id)
            if slot_id
            else f"saloon:{saloon_id}:last_metrics"
        )
        raw_last = redis_client.hgetall(last_key)
        preserve_baseline = False
        fired_temp_alert = None

        if raw_last and "temp_avg" in raw_last:
            delta = float(temp_data["avg"]) - float(raw_last["temp_avg"])
            rate_threshold = _config_float(
                config, "temp_velocity_delta_60s", TEMP_RATE_DELTA_DEFAULT
            )
            if abs(delta) >= rate_threshold:
                suppress, preserve_baseline = _should_suppress_single_slot_test_spike(
                    redis_client,
                    saloon_id,
                    slot_id,
                    "temp",
                    now,
                    grace_sec,
                    still_breaching_baseline=True,
                )
                if not suppress:
                    fired_temp_alert = {
                        "metric": "temp",
                        "value": float(temp_data["avg"]),
                        "tier": 1,
                        "reason": (
                            f"Temperature rate-of-change: {delta:+.1f}°C "
                            f"(threshold {rate_threshold}°C/60s)"
                        ),
                    }
            elif slot_id:
                # Reading is back within normal delta of the preserved baseline!
                # If a grace key was being held, check if a live stream settled/ended
                # and log TEST_SPIKE_IGNORED cleanly.
                grace_key = single_slot_spike_grace_key(saloon_id, slot_id, "temp")
                if redis_client.get(grace_key) is not None:
                    redis_client.delete(grace_key)
                    logger.info(
                        "TEST_SPIKE_IGNORED: saloon=%s slot=%s metric=temp "
                        "(recovered within baseline delta %.2f°C)",
                        saloon_id, slot_id, delta,
                    )

        pipe = redis_client.pipeline()
        if preserve_baseline and raw_last and "temp_avg" in raw_last:
            # Fix 1: Keep the pre-spike temp_avg baseline intact while in grace hold!
            pipe.hset(last_key, mapping={
                "temp_avg": str(raw_last["temp_avg"]),
                "ts": str(now),
            })
        else:
            pipe.hset(last_key, mapping={
                "temp_avg": str(temp_data["avg"]),
                "ts": str(now),
            })
        pipe.expire(last_key, 120)  # TTL: two windows max
        pipe.execute()

        if fired_temp_alert is not None:
            return fired_temp_alert

    return None


def _noop_config(key, default):
    return default
