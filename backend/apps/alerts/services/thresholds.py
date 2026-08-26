"""Threshold hash builder + re-cache service (M4-01, Walkthrough §4.1).

CANON encoding (Walkthrough lines 648–671 — implemented verbatim):
- flat fields ``{metric}_max`` / ``{metric}_min`` from LifecycleThresholdRule
- ``nh3_enabled`` present iff an nh3 rule covers today
- grace key ``threshold:transition:{saloon_id}`` (TTL = seeded
  threshold_transition_grace_sec) written ONLY when the old hash existed AND
  differs — D9 "only-on-change"
- midnight cron loops open flocks; post_save signals fast-path single saloons

Flock-age days are computed in Asia/Tehran local time (Iran is fixed
UTC+03:30, no DST since 2022) so a flock placed at 23:50 UTC still counts as
"day 1" for the farmer whose birds are living at 03:20 Tehran.

فارسی: ساختِ هشِ آستانه‌ها بر اساس روزِ عمرِ جوجه‌ها به وقتِ تهران؛ کلیدِ
گذار فقط وقتی نوشته می‌شود که مقادیر واقعاً عوض شده باشند.
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from apps.alerts.models import (
    LifecycleThresholdRule,
    SaloonOperationalState,
)
from apps.alerts.services import redis_keys as keys
from apps.alerts.services.clock import Clock, system_clock
from apps.farms.models import Saloon
from apps.telemetry.models import FlockCycle
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

# Redis URL read LAZILY (per call) from env so integration tests can point the
# service at an isolated scratch DB via monkeypatched clients or env overrides
# without import-order hazards. Production: unchanged env-provided URL.
_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        import redis as redis_lib

        url = os.environ.get(
            "REDIS_URL", "redis://:dev_redis_password@localhost:6379"
        )
        _redis_client = redis_lib.Redis.from_url(url, decode_responses=True)
    return _redis_client

TEHRAN = ZoneInfo("Asia/Tehran")
GRACE_TTL_FALLBACK = 300  # seeded key threshold_transition_grace_sec


def _grace_ttl_seconds() -> int:
    """Grace TTL from system_configuration (D21); fallback mirrors the seed."""
    from apps.system_config.models import SystemConfiguration

    raw = (
        SystemConfiguration.objects.filter(key="threshold_transition_grace_sec")
        .values_list("value", flat=True)
        .first()
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        return GRACE_TTL_FALLBACK


def _tehran(dt: datetime):
    return dt.astimezone(TEHRAN).date() if dt is not None else None


def flock_age_days(start_date: datetime, now: datetime) -> int:
    """Flock age in WHOLE Tehran-local days (Walkthrough line 653 semantics)."""
    start_local = _tehran(start_date)
    now_local = _tehran(now)
    return (now_local - start_local).days


def _open_flock(saloon_id: int):
    return (
        FlockCycle.objects.filter(saloon_id=saloon_id, end_date__isnull=True)
        .order_by("-start_date")
        .first()
    )


def _latest_flock(saloon_id: int):
    return FlockCycle.objects.filter(saloon_id=saloon_id).order_by("-start_date").first()


def _operational_state(saloon_id: int) -> str:
    latest = (
        SaloonOperationalState.objects.filter(saloon_id=saloon_id)
        .order_by("-entered_at")
        .values_list("state", flat=True)
        .first()
    )
    return latest or "ACTIVE_FLOCK"  # doc default when never transitioned


def build_hash_for_saloon(saloon, clock: Clock | None = None) -> dict | None:
    """Compute the flat threshold-hash dict, or None when inert.

    Inert = never flocked (no rules could ever apply). A FINISHED flock keeps
    its last hash with operational_state=HARVESTED_VACANT (Q3: velocity lane
    stays armed in empty saloons).
    """
    clock = clock or system_clock()
    now_dt = datetime.fromtimestamp(clock.now(), tz=TEHRAN)

    open_flock = _open_flock(saloon.id)
    if open_flock is not None:
        return _hash_for_flock(open_flock, now_dt)

    any_flock = _latest_flock(saloon.id)
    if any_flock is None:
        return None  # never flocked → inert (no thresholds possible)

    # Q3: harvested/vacant — keep whatever flags exist, flip the state field.
    return {"operational_state": "HARVESTED_VACANT"}


def _hash_for_flock(flock: FlockCycle, now_dt) -> dict:
    age_days = flock_age_days(flock.start_date, now_dt)
    template = _resolve_template(flock)
    rules = []
    if template is not None:
        rules = list(
            LifecycleThresholdRule.objects.filter(
                template=template,
                day_from__lte=age_days,
                day_to__gte=age_days,
            )
        )

    mapping: dict[str, str] = {
        "operational_state": "ACTIVE_FLOCK",
        "flock_age_days": str(age_days),
    }
    metrics_seen = set()
    for rule in rules:
        metrics_seen.add(rule.metric)
        if rule.max_value is not None:
            mapping[f"{rule.metric}_max"] = str(rule.max_value)
        if rule.min_value is not None:
            mapping[f"{rule.metric}_min"] = str(rule.min_value)
    if "nh3" in metrics_seen:
        mapping["nh3_enabled"] = "1"  # canon line 658: presence flag only
    return mapping


def _resolve_template(flock: FlockCycle):
    """Resolution chain: org+breed match → org default → global default."""
    from apps.alerts.models import LifecycleTemplate

    qs = LifecycleTemplate.objects.all()
    breed = getattr(flock, "breed_type", None)
    matched = (
        qs.filter(org_id=flock.org_id, breed_type=breed).first()
        or qs.filter(org_id=flock.org_id, is_default=True).first()
        or qs.filter(org__isnull=True, is_default=True).first()
    )
    return matched


def recache_saloon(saloon_id: int, clock: Clock | None = None) -> str:
    """Recompute + write one saloon's hash. Returns 'changed'|'unchanged'|'inert'."""
    client = _redis()
    saloon = Saloon.objects.get(pk=saloon_id)
    new_hash = build_hash_for_saloon(saloon, clock=clock)
    key = keys.thresholds_hash(saloon_id)

    old = client.hgetall(key)
    if new_hash is None:
        if old:
            client.delete(key)
            logger.info("thresholds removed for saloon %s (inert)", saloon_id)
        return "inert"

    changed = bool(old) and {k: v for k, v in old.items()} != {
        k: str(v) for k, v in new_hash.items()
    }
    pipe = client.pipeline()
    pipe.hset(key, mapping={k: str(v) for k, v in new_hash.items()})
    if changed:
        ttl = _grace_ttl_seconds()
        pipe.setex(keys.transition_grace_key(saloon_id), ttl, "1")
    pipe.execute()

    state = "changed" if changed else "unchanged"
    if changed and os.environ.get("M4DEBUG"):
        logger.warning("M4DEBUG old=%r new=%r", old, {k: str(v) for k, v in new_hash.items()})
    logger.info("thresholds %s for saloon %s", state, saloon_id)
    return state


def recache_all_saloons(clock: Clock | None = None) -> int:
    """Midnight backstop: every saloon that has ever had a flock."""
    changed = 0
    for saloon in Saloon.objects.all().iterator():
        if recache_saloon(saloon.pk, clock=clock) == "changed":
            changed += 1
    logger.info("daily threshold sweep: %s hashes changed", changed)
    return changed
