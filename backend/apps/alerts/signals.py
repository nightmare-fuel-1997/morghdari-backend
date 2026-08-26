"""Signal fast-paths — Walkthrough §4.1 lines 667–671.

flock_cycles.post_save (esp. end_date set = flock complete) and
lifecycle_threshold_rules.post_save (NH₃ hardware added mid-day) both trigger
IMMEDIATE re-caches; the midnight cron remains the resilience backstop.
فارسی: سیگنال‌ها مسیرِ سریعِ به‌روزرسانیِ کش هستند و کرن نیمه‌شب پشتیبان.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.alerts.models import LifecycleThresholdRule
from apps.alerts.tasks import cache_daily_thresholds, cache_saloon_thresholds
from apps.telemetry.models import FlockCycle

logger = logging.getLogger(__name__)


@receiver(post_save, sender=FlockCycle, dispatch_uid="alerts_flock_recache")
def flock_saved(sender, instance, created, **kwargs):
    """Fast path on EVERY flock save: placement starts day 1; setting
    end_date must re-arm the hash the moment it happens (Walkthrough line 646)."""
    cache_saloon_thresholds.delay(str(instance.saloon_id))


@receiver(
    post_save,
    sender=LifecycleThresholdRule,
    dispatch_uid="alerts_rules_recache",
)
def rules_changed(sender, instance, created, **kwargs):
    """Rule edits affect every active saloon using the template — run the full
    sweep asynchronously rather than guessing affected saloons here."""
    cache_daily_thresholds.delay()
