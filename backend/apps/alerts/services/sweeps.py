"""Alert lifecycle sweeps — escalation heartbeat (M4-05); LOS/replay follow
in M4-07/M4-08. These functions run as thin Celery task bodies; all business
logic lives here. Every sweep is idempotent: calling twice has the same effect
as calling once.

Escalation canon (TUTORIAL §3.4 timeline): Tier 1 fires inline via evaluation;
the 2-minute sweep promotes Tier 2 at 15 min and Tier 3 at 30 min of
UNRESOLVED breaching — never demotes, skips ack-paused alerts, resolves when
track_breach deleted the breach key (hysteresis passed), and NEVER touches
REPLAY_ANALYSIS rows (labeled historical, never pushed — DEMO §2).
فارسی: ضربانِ ارتقا — سطح ۲ در دقیقهٔ ۱۵ و سطح ۳ در دقیقهٔ ۳۰؛ هرگز تنزل نیست،
هشدارهای بی‌صدا‌شده را رد می‌کند و ردیف‌های بازپخش را لمس نمی‌کند.
"""

import logging

from django.db import transaction
from django.utils import timezone as dj_tz

from apps.alerts.models import AlertInstance
from apps.alerts.services.breach import breach_value
from apps.alerts.services.redis_keys import (
    active_alert_key,
    breach_key,
    paused_key,
    sensor_status_key,
)

logger = logging.getLogger(__name__)

# Delivery sinks per tier — REAL channels land in v0.6/v0.7; v0.4 logs the
# routing decision so the timeline is observable end-to-end.
DELIVERY_SINKS = {1: "push_workers", 2: "sms_engineer", 3: "manager_and_doctor"}


def _config_int(key: str, default: int) -> int:
    from apps.system_config.models import SystemConfiguration

    raw = SystemConfiguration.objects.filter(key=key).values_list("value", flat=True).first()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def system_redis():
    """Redis connection for sweeps (same URL as consumers/subscriber)."""
    import os

    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True)


def _save_alert(alert, fields: list[str]) -> None:
    """Persist inside the alert's own tenant context — sweeps cross orgs, and
    the worker connection runs RLS-gated exactly like request traffic."""
    from apps.tenants.context import tenant_context

    with transaction.atomic():
        with tenant_context(str(alert.org_id)):
            alert.save(update_fields=fields)


def escalate_all(now_dt=None) -> int:
    """Main 2-minute sweep: promote tiers, resolve recovered, honor pauses.

    Returns number of alerts whose status changed.
    فارسی: چرخشِ دومین‌نامهٔ ارتقا؛ خروجی تعدادِ تغییرات است.
    """
    client = system_redis()
    now = now_dt or dj_tz.now()

    tier2_sec = max(_config_int("escalation_tier2_min", 15), 1) * 60
    tier3_sec = max(_config_int("escalation_tier3_min", 30), 1) * 60

    active = (
        AlertInstance.objects
        .filter(resolved_at__isnull=True)
        .exclude(source="REPLAY_ANALYSIS")
    )
    changed = 0

    for alert in active:
        if client.exists(paused_key(str(alert.id))):
            logger.debug("alert %s paused — escalation timer frozen", alert.id)
            continue

        bkey = breach_key(alert.saloon_id, alert.metric_type)

        if not client.exists(bkey):
            # Recovery: track_breach deletes the key only after the value
            # dropped past hysteresis — seeing no key means genuinely better.
            alert.current_status = "RESOLVED"
            alert.resolved_at = now
            _save_alert(alert, ["current_status", "resolved_at"])
            client.delete(active_alert_key(alert.saloon_id, alert.metric_type))
            logger.info("alert %s resolved (metric recovered)", alert.id)
            changed += 1
            continue

        latest = breach_value(client, alert.saloon_id, alert.metric_type)
        elapsed = (now - alert.triggered_at).total_seconds()

        # Promotion only — a tier already delivered is never taken back.
        current_tier = alert.highest_tier_reached or 1
        new_status = None
        new_tier = current_tier
        if elapsed >= tier3_sec and current_tier < 3:
            new_status, new_tier = "ACTIVE_TIER_3", 3
        elif elapsed >= tier2_sec and current_tier < 2:
            new_status, new_tier = "ACTIVE_TIER_2", 2

        if new_status:
            alert.current_status = new_status
            alert.highest_tier_reached = new_tier
            fields = ["current_status", "highest_tier_reached"]
            if latest is not None:
                alert.current_value = latest
                fields.append("current_value")
            _save_alert(alert, fields)
            _deliver_notification(alert, new_tier)
            changed += 1
        elif latest is not None and float(alert.current_value or 0) != latest:
            # Keep current_value fresh between promotions (exact rebuild data).
            alert.current_value = latest
            _save_alert(alert, ["current_value"])

    return changed


def _deliver_notification(alert, tier: int) -> None:
    """Delivery STUB (M4-05): log the routing decision per tier. Real push/SMS
    channels arrive with the v0.6 contract surface."""
    sink = DELIVERY_SINKS.get(tier, f"unknown_tier_{tier}")
    logger.info(
        "ALERT DELIVERY [%s]: saloon=%s metric=%s value=%s tier=%d",
        sink, alert.saloon_id, alert.metric_type, alert.current_value, tier,
    )


def los_watchdog(now_dt=None) -> int:
    """60-second LOS sweep: provisioned slots whose sensor:status expired ⇒ LOS.

    Suppressed during redis-restart window (los_suppress_after_redis_restart_min).
    Full acceptance (300 s grace, dedup, restart suppression tests) lands M4-07.
    فارسی: سگمنتِ از دست رفتنِ دادهٔ حسگر — اسلات‌های بدونِ کلید وضعیت هشدار می‌دهند.
    """
    from apps.devices.models import SaloonSensorSlot

    client = system_redis()
    suppress_min = _config_int("los_suppress_after_redis_restart_min", 10)
    uptime_sec = client.info("server").get("uptime_in_seconds", 0)
    if uptime_sec < suppress_min * 60:
        logger.debug("redis recently restarted (%ds) — suppressing LOS sweep", uptime_sec)
        return 0

    fired = 0
    for slot in SaloonSensorSlot.objects.iterator():
        skey = sensor_status_key(slot.saloon_id, slot.pk)
        if client.exists(skey):
            continue
        logger.warning(
            "LOS detected: slot=%s saloon=%s has no recent sensor data",
            slot.pk, slot.saloon_id,
        )
        from apps.tenants.context import tenant_context

        with transaction.atomic():
            with tenant_context(str(slot.org_id)):
                AlertInstance.objects.create(
                    org=slot.org,
                    farm_id=slot.saloon.farm_id,
                    saloon=slot.saloon,
                    metric_type="los",
                    current_status="ACTIVE_TIER_1",
                    current_value=None,
                    source="REALTIME",
                )
        fired += 1

    return fired
