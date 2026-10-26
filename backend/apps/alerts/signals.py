"""Signal fast-paths — Walkthrough §4.1 lines 667–671 (+ v1.7.1 templates).

flock_cycles.post_save (esp. end_date set = flock complete) and
lifecycle_threshold_rules.post_save (NH₃ hardware added mid-day) both trigger
IMMEDIATE re-caches; the midnight cron remains the resilience backstop.

v1.7.1 (2026-09-12) adds the two write-surface fast paths, kept in the SAME
shape as the pair above — a published template re-runs the full sweep, an
explicit saloon assignment rebuilds exactly that one saloon:

  lifecycle_templates.post_save  PUBLISHED only (a draft edit must not sweep —
                                it cannot change any hash in the first place)
  saloons.post_save              only when `threshold_template` moved

فارسی: سیگنال‌ها مسیرِ سریعِ به‌روزرسانیِ کش هستند و کرن نیمه‌شب پشتیبان؛
انتشارِ قالب کلِ جاروب را اجرا می‌کند و تخصیصِ سالن فقط همان سالن را.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule
from apps.alerts.tasks import cache_daily_thresholds, cache_saloon_thresholds
from apps.farms.models import Saloon
from apps.telemetry.models import FlockCycle

logger = logging.getLogger(__name__)


def _dispatch(task, *args) -> None:
    """Enqueue a recache ONLY after the writing transaction commits.

    D22 (2026-10-26, found live on 82.115.17.252): every one of these handlers
    fires inside the caller's transaction — `saloon_create`, the flock
    placement/close writers and the template writers all wrap their writes in
    `transaction.atomic()`. Dispatching immediately let a worker pick the task
    up before COMMIT, where a different connection cannot see the row yet:
    `cache_saloon_thresholds` died with `DoesNotExist('Saloon matching query
    does not exist')` — 8 times in one afternoon of QA — and the hash it was
    supposed to rebuild stayed stale until the next trigger, which is the
    exact failure mode D5 was about.

    `on_commit` is the same rule `issue_command` already follows for its
    publish: a rolled-back request leaves no work behind, and a committed one
    always sees its own row.
    """
    transaction.on_commit(lambda: task.delay(*args))


@receiver(post_save, sender=FlockCycle, dispatch_uid="alerts_flock_recache")
def flock_saved(sender, instance, created, **kwargs):
    """Fast path on EVERY flock save: placement starts day 1; setting
    end_date must re-arm the hash the moment it happens (Walkthrough line 646)."""
    _dispatch(cache_saloon_thresholds, str(instance.saloon_id))


@receiver(
    post_save,
    sender=LifecycleThresholdRule,
    dispatch_uid="alerts_rules_recache",
)
def rules_changed(sender, instance, created, **kwargs):
    """Rule edits affect every active saloon using the template — run the full
    sweep asynchronously rather than guessing affected saloons here."""
    _dispatch(cache_daily_thresholds)


@receiver(
    post_save,
    sender=LifecycleTemplate,
    dispatch_uid="alerts_template_recache",
)
def template_changed(sender, instance, created, **kwargs):
    """Publish/rule-shape changes: full sweep, same reasoning as rule edits.

    DRAFTS ARE SKIPPED on purpose. A draft is invisible to
    `effective_template`, so its bands cannot reach any hash — sweeping for it
    would be pure waste. The guard is also the safety property stated in the
    model docstring: an engineer authoring a draft cannot move a live band.
    """
    if instance.status != LifecycleTemplate.PUBLISHED:
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and not (
        {"status", "is_default", "breed_type", "org_id"} & set(update_fields)
    ):
        return  # a rename cannot move a band — skip the sweep
    _dispatch(cache_daily_thresholds)


@receiver(post_save, sender=Saloon, dispatch_uid="alerts_saloon_template_recache")
def saloon_assignment_changed(sender, instance, created, **kwargs):
    """Explicit assignment is the only Saloon field that can change a
    threshold hash, so a targeted save (update_fields) that does not touch it
    is ignored. A full save re-caches anyway: silent cache drift is worse than
    redundant work (the same trade the midnight cron is built on)."""
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "threshold_template" not in update_fields:
        return
    _dispatch(cache_saloon_thresholds, str(instance.pk))
