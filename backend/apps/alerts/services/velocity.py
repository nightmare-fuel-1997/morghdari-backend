"""Velocity emergencies — Decision 10: evaluated BEFORE any suppression.

Two lanes, checked in order per enriched reading:
  1. NH₃ max_ts spike: any metric whose reading carries max_ts ≥
     nh3_max_ts_velocity_threshold fires INSTANT Tier 1. This means the sensor
     caught a SPIKE mid-window, not a slow drift. Bypasses grace keys, mutes,
     operational state — every suppression gate.
  2. Temperature rate-of-change: current avg vs cached last avg must differ by
     ≥ temp_velocity_delta_60s °C within one window.

Both lanes gated by `{metric}_enabled` flags from the threshold hash.
NH₃ invariant: ALWAYS evaluates `.max`, never `.avg` (Saas.md mandate).
فارسی: بررسیِ سرعت‌های خطرناک قبل از همهٔ فیلترها؛ آمونیاک همیشه با مقدارِ بیشینه
سنجیده می‌شود و تغییرِ ناگهانیِ دما هم فوراً هشدار می‌دهد.
"""

import json
import logging

logger = logging.getLogger(__name__)

NH3_MAX_TS_THRESHOLD_DEFAULT = 50
TEMP_RATE_DELTA_DEFAULT = 5.0


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


def check_velocity_emergency(
    redis_client,
    saloon_id: int,
    thresholds: dict,
    metrics: dict,
    clock_now: float | None = None,
    config_getter=None,
) -> dict | None:
    """Evaluate BOTH velocity lanes against the enriched reading's metrics.

    Returns an alert-dict when an emergency fires, or None when all values
    are within normal operational bounds. Never raises.
    """
    config = config_getter or _noop_config

    # ── Lane 1: NH₃ max_ts spike (Decision 10: runs FIRST, before ALL suppression)
    nh3_enabled = thresholds.get("nh3_enabled") == "1"
    nh3_data = metrics.get("nh3")
    if nh3_enabled and isinstance(nh3_data, dict):
        max_ts = nh3_data.get("max_ts")
        spike_threshold = _config_int(
            config, "nh3_max_ts_velocity_threshold", NH3_MAX_TS_THRESHOLD_DEFAULT
        )
        if max_ts is not None and int(max_ts) >= spike_threshold:
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
        last_key = f"saloon:{saloon_id}:last_metrics"
        raw_last = redis_client.hgetall(last_key)

        if raw_last and "temp_avg" in raw_last:
            delta = float(temp_data["avg"]) - float(raw_last["temp_avg"])
            rate_threshold = _config_float(
                config, "temp_velocity_delta_60s", TEMP_RATE_DELTA_DEFAULT
            )
            if abs(delta) >= rate_threshold:
                return {
                    "metric": "temp",
                    "value": float(temp_data["avg"]),
                    "tier": 1,
                    "reason": (
                        f"Temperature rate-of-change: {delta:+.1f}°C "
                        f"(threshold {rate_threshold}°C/60s)"
                    ),
                }

        # update the cache for the NEXT reading's rate calculation
        import time as _time

        pipe = redis_client.pipeline()
        pipe.hset(last_key, mapping={
            "temp_avg": str(temp_data["avg"]),
            "ts": str(clock_now or _time.time()),
        })
        pipe.expire(last_key, 120)  # TTL: two windows max
        pipe.execute()

    return None


def _noop_config(key, default):
    return default
