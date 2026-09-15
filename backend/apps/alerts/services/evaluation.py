"""Alert evaluation — inline check on every drained realtime entry (M4-04).

Called AFTER the telemetry row is committed but BEFORE the entry is acked.
If the reading breaches its cached threshold AND the breach duration filter
passes, fires (or coalesces) an AlertInstance row inside tenant_context.

Evaluation order (Decision 10 / Walkthrough §4.2):
  ① Velocity emergency → instant Tier 1 (bypasses ALL suppression)
  ② Midnight transition grace → suppresses non-velocity
  ②b Maintenance mute window → suppresses threshold/duration alerting
  ②c Subscription mute → suppresses muted CATEGORIES (v0.9/B2 — org-wide
      AlertSubscription rows; the velocity lane already returned above, so
      Tier-1 velocity is structurally unmutable; LOS/escalation untouched)
  ③ Threshold comparison + breach accumulation + 900 s duration gate + hysteresis
  ④ D23 capability-gated derived rules (THI — muted as a category like the rest)

فارسی: ارزیابیِ هشدار بعد از درجِ تله‌متری و قبل از امضا؛ اولویت با سرعت است.
"""

import logging
import time
from datetime import datetime, timezone as dt_timezone

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

    # ── ②b GS-MAINT-MUTE (M4-10): planned-maintenance suppression ──────────
    # Threshold-crossing + duration-filter alerting is silenced while a mute
    # window is active; readings still log (telemetry committed earlier) and
    # every suppressed alert feeds the per-window digest. Velocity emergencies
    # NEVER reach here — lane ① already returned (Decision 10 invariant).
    from apps.alerts.services.mutes import active_maintenance_mute, note_suppressed

    now_dt = datetime.fromtimestamp(
        clock_now if clock_now is not None else time.time(), tz=dt_timezone.utc,
    )
    mute = active_maintenance_mute(saloon_id, now_dt)
    if mute is not None:
        note_suppressed(mute, now_dt)
        logger.debug(
            "maintenance mute %s active for saloon %s — threshold alerting suppressed",
            mute.id, saloon_id,
        )
        return None

    # ── ②c subscription mute (v0.9 M9-10/B2): org-wide category switch ────
    # Muted categories never accumulate breach (unmuting starts fresh) and
    # every suppressed fire feeds the Redis digest. Velocity NEVER reaches
    # here (lane ① returned); LOS/escalation live outside this function.
    from apps.alerts.services.subscriptions import muted_metrics, note_sub_muted

    muted = muted_metrics(org_id)

    # ── ③ threshold comparison + breach accumulation + duration gate ──────
    duration_sec = _config_int("breach_duration_sec", 900)
    hysteresis = _config_float("hysteresis_buffer_units", 0.5)

    from apps.alerts.services.firing import fire_or_coalesce

    fired = None
    for metric_key, metric_values in metrics.items():
        if not isinstance(metric_values, dict):
            continue
        if metric_key in muted:
            note_sub_muted(redis_client, org_id, metric_key)
            logger.debug(
                "subscription mute suppresses %s for org %s", metric_key, org_id
            )
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

        # Duration filter (TUTORIAL §3.2): Tier 1 fires exactly once per
        # episode, only after breach_duration_sec of SUSTAINED breach.
        if consume_fire(redis_client, saloon_id, metric_key,
                        duration_sec, clock_now=clock_now):
            fire = fire_or_coalesce(
                redis_client, org_id, farm_id, saloon_id,
                metric=metric_key, value=float(metric_values["avg"]),
                clock_now=clock_now,
            )
            fired = {**fire, "metric": metric_key, "tier": 1}

    # ── ④ D23 capability-gated derived rules (M4-09) ───────────────────────
    # THI heat-stress: rule exists (thi_max in the hash) but the index is
    # DERIVED — computed only when temp AND humidity are co-present in this
    # reading (D23: "live temp + humidity"). One input missing ⇒ unavailable,
    # never assumed normal. Sensor-scarce keys (co/power_*) need no extra
    # branch: the ③ loop above already evaluates only keys the entry carries.
    from apps.alerts.services.capability import compute_thi, metric_is_live

    thi_max_str = thresholds.get("thi_max")
    if thi_max_str is not None and metric_is_live(metrics, "temp") \
            and metric_is_live(metrics, "humidity"):
        # BUG-20 (QA 8.13): a muted 'thi' used to fall through to
        # track_breach with `thi` unbound (computed only in the unmuted
        # else) → UnboundLocalError crashed the drain on every THI-capable
        # reading. Muted means note + SKIP — the same semantics as lane ③:
        # no phantom breach episode, digest still fed (D24-6).
        # فارسی: بی‌صدا شدنِ thi باید مثل بند ③ کامل رد شود، نه سقوطِ زنجیره.
        if "thi" in muted:
            note_sub_muted(redis_client, org_id, "thi")
        else:
            thi = compute_thi(float(metrics["temp"]["avg"]),
                              float(metrics["humidity"]["avg"]))
            result = track_breach(
                redis_client, saloon_id, "thi", thi,
                float(thi_max_str), hysteresis, duration_sec, clock_now=clock_now,
            )
            if result == "breaching" and consume_fire(
                redis_client, saloon_id, "thi", duration_sec, clock_now=clock_now
            ):
                fire = fire_or_coalesce(
                    redis_client, org_id, farm_id, saloon_id,
                    metric="thi", value=thi, clock_now=clock_now,
                )
                fired = {**fire, "metric": "thi", "tier": 1}

    return fired
