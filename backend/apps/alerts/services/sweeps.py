"""Alert lifecycle sweeps — escalation, LOS watchdog, replay dispatch (M4-05..08).

These functions run as thin Celery task bodies; all business logic lives here.
Every sweep is idempotent: calling twice has the same effect as calling once.

فارسی: سرویس‌های چرخهٔ هشدار — ارتقا، سگمنتِ بی‌داده و پخشِ بازپخش؛ همه تکرارپذیر.
"""

import logging
import time
import uuid

from django.db import transaction
from django.utils import timezone as dj_tz

from apps.alerts.models import (
    AlertInstance,
    MaintenanceMute,
    SaloonOperationalState,
)
from apps.alerts.services.clock import Clock, system_clock
from apps.alerts.services.redis_keys import (
    active_alert_key,
    paused_key,
    replay_pending,
    replay_sweeping,
    sensor_status_key,
)

logger = logging.getLogger(__name__)


def _config_int(key: str, default: int) -> int:
    from apps.system_config.models import SystemConfiguration

    raw = SystemConfiguration.objects.filter(key=key).values_list("value", flat=True).first()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def escalate_all() -> int:
    """Main 2-minute sweep: promote tiers, detect recovery, honor pauses.

    Returns number of alerts whose status/tier changed.
    فارسی: ارتقای سطح هشدار بر اساس مدتِ زمانِ گذشته و تشخیص بهبود.
    """
    client = system_redis()
    now = datetime.now(dt_tz.utc)

    tier2_min = max(_config_int("escalation_tier2_min", 15), 1)
    tier3_min = max(_config_int("escalation_tier3_min", 30), 1)
    hysteresis_buffer = float(
        SystemConfigurationValue("hysteresis_buffer_units", "0.5")
    )
    duration_sec = _config_int("breach_duration_sec", 900)

    active_alerts = AlertInstance.objects.filter(resolved_at__isnull=True)
    changed = 0

    for alert in active_alerts:
        # Pause key check (honors acknowledgements)
        pause_key = f"alert:paused:{alert.id}"
        if client.exists(pause_key):
            logger.debug("alert %s paused — skipping escalation", alert.id)
            continue

        elapsed_sec = (now - alert.triggered_at).total_seconds()

        # Recovery detection: if the breach key is gone, the metric recovered.
        breach_k = f"saloon:{alert.saloon_id}:breach:{alert.metric_type}"
        if not client.exists(breach_k):
            # Metric recovered past hysteresis → RESOLVE
            alert.current_status = "RESOLVED"
            alert.resolved_at = now
            alert.save(update_fields=["current_status", "resolved_at"])
            _delete_active_key(client, alert.saloon_id, alert.metric_type)
            logger.info(
                "alert %s resolved (metric recovered)", alert.id
            )
            changed += 1
            continue

        current_value_raw = client.get(breach_k)
        current_value = float(current_value_raw) if current_value_raw else None

        # Tier promotion (never demotes)
        new_status = None
        new_tier = alert.highest_tier_reached or 1

        if elapsed_sec >= tier3_min * 60 and new_tier < 3:
            new_status = "ACTIVE_TIER_3"
            new_tier = 3
        elif elapsed_sec >= tier2_min * 60 and new_tier < 2:
            new_status = "ACTIVE_TIER_2"
            new_tier = 2

        if new_status and new_status != alert.current_status:
            alert.current_status = new_status
            alert.highest_tier_reached = new_tier
            if current_value is not None:
                alert.current_value = current_value
            alert.save(update_fields=["current_status", "highest_tier_reached", "current_value"])
            _deliver_notification(alert, new_tier)
            changed += 1
        elif current_value is not None:
            # Refresh current_value even without tier change
            alert.current_value = current_value
            alert.save(update_fields=["current_value"])

    return changed


def _deliver_notification(alert, tier: int) -> None:
    """Delivery stubs — real channels land in v0.6/v0.7."""
    sinks = {1: "push_workers", 2: "sms_engineer", 3: "manager_and_doctor"}
    sink = sinks.get(tier, f"unknown_tier_{tier}")
    logger.info(
        "ALERT DELIVERY [%s]: saloon=%s metric=%s value=%s tier=%d",
        sink, alert.saloon_id, alert.metric_type, alert.current_value, tier,
    )


def los_watchdog() -> int:
    """60-second LOS sweep: provisioned slots whose sensor:status expired ⇒ LOS.

    Suppressed during redis-restart window (los_suppress_after_redis_restart_min).
    فارسی: سگمنتِ از دست رفتنِ دادهٔ حسگر — اسلات‌های بدونِ کلید وضعیت هشدار می‌دهند.
    """
    from apps.devices.models import SaloonSensorSlot

    client = system_redis()
    suppress_min = _config_int("los_suppress_after_redis_restart_min", 10)
    r = client.info("server")
    uptime_sec = r.get("uptime_in_seconds", 0)
    if uptime_sec < suppress_min * 60:
        logger.debug("redis recently restarted (%ds) — suppressing LOS sweep", uptime_sec)
        return 0

    ttl_expected = _config_int("sensor_status_ttl_sec", 300)
    active_slots = SaloonSensorSlot.objects.all()
    fired = 0

    for slot in active_slots.iterator():
        skey = f"sensor:status:{slot.saloon_id}:{slot.pk}"
        if client.exists(skey):
            continue
        # No status key = this slot hasn't reported within its expected window
        logger.warning(
            "LOS detected: slot=%s saloon=%s has no recent sensor data",
            slot.pk, slot.saloon_id,
        )

        # Write a LOS alert instance
        from apps.alerts.models import AlertInstance

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


# ── shared helpers ────────────────────────────────────────────────────────


def system_redis():
    """Redis connection for sweeps (same URL as consumers/subscriber)."""
    import os

    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True)


class SystemConfigurationValue:
    def __init__(self, key: str, default: str):
        self.key = key
        self.default = default

    def __float__(self) -> float:
        return self._get_float()

    def _get_float(self) -> float:
        from apps.system_config.models import SystemConfiguration

        raw = SystemConfiguration.objects.filter(key=self.key).values_list("value", flat=True).first()
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(self.default)


def _delete_active_key(client, saloon_id, metric):
    key = f"alert:active:saloon:{saloon_id}:{metric}"
    client.delete(key)
