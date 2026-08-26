"""Alerts Celery tasks — Blueprint §5 CANONICAL names (drift is over).

Bodies are intentionally thin in M4-00: each one delegates to a service
function that later milestones fill in (M4-01 thresholds, M4-02 breach
accumulation, M4-05 escalation sweep, M4-07 LOS watchdog, M4-08 replay sweep).
Registering the canonical names NOW pins the topology and lets beat entries
land per-task without renaming churn.

فارسی: نام‌های کانونیکلِ تسک‌های هشدار طبق Blueprint §5؛ بدنه‌ها در مایل‌استون‌های
بعدی پر می‌شوند.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="alerts.cache_daily_thresholds")
def cache_daily_thresholds():
    """Midnight (Asia/Tehran) backstop: rebuild every saloon threshold hash.

    Primary refresh paths are the flock_cycles / lifecycle_threshold_rules
    post_save signals; the cron exists because silent cache drift is worse
    than redundant work (Walkthrough §6).
    """
    from apps.alerts.services.thresholds import recache_all_saloons

    return recache_all_saloons()


@shared_task(name="alerts.cache_saloon_thresholds")
def cache_saloon_thresholds(saloon_id: int):
    """Targeted re-cache after flock_cycles / lifecycle_threshold_rules changes."""
    from apps.alerts.services.thresholds import recache_saloon

    return recache_saloon(saloon_id)


@shared_task(name="alerts.escalate_alerts")
def escalate_alerts():
    """2-minute sweep: promote tiers, resolve recovered, honor pause keys."""
    from apps.alerts.services.sweeps import escalate_all

    return escalate_all()


@shared_task(name="alerts.los_watchdog_sweep")
def los_watchdog_sweep():
    """60-second LOS watchdog: expired sensor:status keys ⇒ LOS alerts."""
    from apps.alerts.services.sweeps import los_watchdog

    return los_watchdog()


@shared_task(name="alerts.run_replay_alert_sweep")
def run_replay_alert_sweep(gateway_slot: str | None = None):
    """Retroactive REPLAY_ANALYSIS alerts once a backlog fully drains."""
    from apps.alerts.services.replay import run_replay_sweep

    return run_replay_sweep(gateway_slot)


@shared_task(name="alerts.rebuild_active_state_from_db")
def rebuild_active_state_from_db():
    """PARKED to M8 (owner decision Q10, Tracker §4l) — DR drill scope."""
    raise NotImplementedError("parked to M8 DR drill (Tracker §4l Q10)")
