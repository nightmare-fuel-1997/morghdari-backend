"""Alert evaluation — inline check on every drained realtime entry (M4-04).

Called AFTER the telemetry row is committed but BEFORE the entry is acked.
If the reading breaches its cached threshold AND the breach duration filter
passes, fires (or coalesces) an AlertInstance row inside tenant_context.

Evaluation order (Decision 10 / Walkthrough §4.2):
  ① Velocity emergency → instant Tier 1 (bypasses ALL suppression)
  ② Midnight transition grace → suppresses non-velocity
  ③ Threshold comparison + breach accumulation + 900 s duration gate + hysteresis

فارسی: ارزیابیِ هشدار بعد از درجِ تله‌متری و قبل از امضا؛ اولویت با سرعت است.
"""

import logging
import time

from apps.alerts.services.breach import consume_fire, track_breach
from apps.alerts.services.redis_keys import thresholds_hash

logger = logging.getLogger(__name__)


def _config_int(key: str, default: int) -> int:
    from apps.system_config.models import SystemConfiguration

    raw = SystemConfiguration.objects.filter(key=key).values_list("value", flat=True).first()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _config_float(key: str, default: float) -> float:
    from apps.system_config.models import SystemConfiguration

    raw = SystemConfiguration.objects.filter(key=key).values_list("value", flat=True).first()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def evaluate_entry(
    redis_client,
    saloon_id: int,
    org_id: str,
    farm_id: int,
    slot_id: str,
    metrics: dict,
    device_ts: str,
    clock_now: float | None = None,
) -> dict | None:
    """Evaluate one enriched entry against cached thresholds.

    Returns an alert-dict when something fires, else None.
    فارسی: یک ردیفِ داده را با آستانه‌های کش‌شده می‌سنجد.
    """
    thresholds_hash_key = thresholds_hash(saloon_id)
    thresholds = redis_client.hgetall(thresholds_hash_key)
    if not thresholds:
        return None  # no thresholds cached ⇒ no alerts possible (fail-silent)

    clock_now = time.time() if clock_now is None else clock_now

    # ── ① velocity emergency (bypasses ALL suppression) ────────────────────
    from apps.alerts.services.velocity import check_velocity_emergency
    from apps.system_config.models import SystemConfiguration as _SC

    def cfg_get(k, d):
        return (
            _SC.objects.filter(key=k).values_list("value", flat=True).first() or d
        )

    velocity_result = check_velocity_emergency(
        redis_client, saloon_id, thresholds, metrics,
        clock_now=clock_now, config_getter=cfg_get,
    )
    if velocity_result is not None:
        from apps.alerts.services.firing import fire_or_coalesce

        fire = fire_or_coalesce(
            redis_client, org_id, farm_id, saloon_id,
            metric=velocity_result["metric"],
            value=velocity_result.get("value", 0),
            clock_now=clock_now,
            reason=velocity_result.get("reason", "velocity"),
        )
        return {**velocity_result, **fire}

    # ── ② midnight grace suppresses non-velocity ──────────────────────────
    from apps.alerts.services.grace import is_graced

    if is_graced(redis_client, saloon_id):
        logger.debug("grace key active for saloon %s — suppressing non-velocity", saloon_id)
        return None

    # ── ③ threshold comparison + breach accumulation + duration gate ──────
    duration_sec = _config_int("breach_duration_sec", 900)
    hysteresis = _config_float("hysteresis_buffer_units", 0.5)

    from apps.alerts.services.firing import fire_or_coalesce

    fired = None
    for metric_key, metric_values in metrics.items():
        if not isinstance(metric_values, dict):
            continue
        max_str = thresholds.get(f"{metric_key}_max")
        if max_str is None:
            continue
        max_threshold = float(max_str)

        result = track_breach(
            redis_client, saloon_id, metric_key,
            float(metric_values["avg"]), max_threshold,
            hysteresis, duration_sec, clock_now=clock_now,
        )
        if result != "breaching":
            continue

        # Duration filter (TUTORIAL §3.3): Tier 1 fires exactly once per
        # episode, only after breach_duration_sec of SUSTAINED breach.
        if consume_fire(redis_client, saloon_id, metric_key,
                        duration_sec, clock_now=clock_now):
            fire = fire_or_coalesce(
                redis_client, org_id, farm_id, saloon_id,
                metric=metric_key, value=float(metric_values["avg"]),
                clock_now=clock_now,
            )
            fired = {**fire, "metric": metric_key, "tier": 1}

    return fired
