"""Threshold hash builder + re-cache service (M4-01, Walkthrough §4.1).

CANON encoding (Walkthrough lines 648–671 — implemented verbatim):
- flat fields ``{metric}_max`` / ``{metric}_min`` from LifecycleThresholdRule
- ``nh3_enabled`` present iff an nh3 rule covers today
- grace key ``threshold:transition:{saloon_id}`` (TTL = seeded
  threshold_transition_grace_sec) written ONLY when the old hash existed AND
  differs — D9 "only-on-change"
- midnight cron loops open flocks; post_save signals fast-path single saloons
- template resolution is the FIVE-step chain documented on
  ``effective_template`` (assignment → org+breed → org default → global default
  → none) and it considers PUBLISHED rows only — a draft (v1.7.1) can never arm
  a saloon, which is exactly why opening the template write surface was safe

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
    LifecycleTemplate,
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
    """Client for the CURRENT env URL — re-binds if REDIS_URL changes between
    modules (pytest collection imports every test module before ANY test runs,
    so last-writer-wins env stomping must not freeze the first caller's DB).
    Production: env never changes → identical to a plain lazy singleton."""
    global _redis_client
    url = os.environ.get(
        "REDIS_URL", "redis://:dev_redis_password@localhost:6379"
    )
    if _redis_client is None or getattr(_redis_client, "_bound_url", None) != url:
        import redis as redis_lib

        _redis_client = redis_lib.Redis.from_url(url, decode_responses=True)
        _redis_client._bound_url = url
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
    # M8-03b (T3 live 2026-09-12): same admin-alias routing as the saloon
    # listing — flock_cycles is FORCE-RLS'd, so a default-alias read from a
    # GUC-less worker sees ZERO rows and every saloon goes inert.
    return (
        FlockCycle.objects.using(_alias()).filter(saloon_id=saloon_id,
        end_date__isnull=True)
        .order_by("-start_date")
        .first()
    )


def _latest_flock(saloon_id: int):
    # Same M8-03b alias routing as _open_flock (see above).
    return FlockCycle.objects.using(_alias()).filter(saloon_id=saloon_id).order_by("-start_date").first()


def _operational_state(saloon_id: int) -> str:
    latest = (
        SaloonOperationalState.objects.using(_alias())
        .filter(saloon_id=saloon_id)
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
        return _hash_for_flock(open_flock, now_dt, saloon=saloon)

    any_flock = _latest_flock(saloon.id)
    if any_flock is None:
        return None  # never flocked → inert (no thresholds possible)

    # Q3 (Tracker §4l): a FINISHED flock keeps its hash with
    # operational_state=HARVESTED_VACANT and the *_enabled flags INTACT —
    # the velocity lane stays armed in empty saloons; only never-flocked
    # saloons have no hash (inert).
    previous = _redis().hgetall(keys.thresholds_hash(saloon.id))
    flags = {k: v for k, v in previous.items() if k.endswith("_enabled")}
    return {"operational_state": "HARVESTED_VACANT", **flags}


def _hash_for_flock(flock: FlockCycle, now_dt, saloon=None) -> dict:
    age_days = flock_age_days(flock.start_date, now_dt)
    template, _source = effective_template(flock, saloon)
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


# Resolution sources, in chain order. The string is CONTRACT (it is what the
# saloon-assignment endpoint reports as `source`), so it is defined once here.
SOURCE_ASSIGNED = "assigned"
SOURCE_ORG_BREED = "org_breed"
SOURCE_ORG_DEFAULT = "org_default"
SOURCE_GLOBAL_DEFAULT = "global_default"
SOURCE_NONE = "none"


def _assigned_template_id(flock, saloon):
    """The saloon's EXPLICIT assignment, or None. Uses the loaded saloon when
    the caller has one (the re-cache path always does) and otherwise reads just
    that one column — callers like KPI compliance only hold the flock."""
    if saloon is not None:
        return getattr(saloon, "threshold_template_id", None)
    saloon_id = getattr(flock, "saloon_id", None)
    if saloon_id is None:
        return None
    return (
        Saloon.objects.using(_alias())
        .filter(pk=saloon_id)
        .values_list("threshold_template_id", flat=True)
        .first()
    )


def effective_template(flock, saloon=None) -> tuple[LifecycleTemplate | None, str]:
    """THE resolution chain — returns ``(template | None, source)``.

    assignment → org+breed → org default → global default → none.

    Two invariants are load-bearing:

    * **Only PUBLISHED rows arm anything.** A DRAFT never resolves, which is
      precisely what makes it safe for an engineer to author one — the band a
      live saloon is evaluated against cannot change behind a manager's back.
    * **Deterministic.** The ordering is explicit (`-is_default, template_id`),
      so a choice is never left to DB row order. DB-level uniqueness guards
      (alerts/0005) make a tie a data error rather than a coin toss.

    An assignment whose template is foreign, unpublished or deleted falls
    through to the chain instead of arming nothing: a broken assignment must
    degrade to the previous behaviour, never to silence.
    """
    org_id = getattr(flock, "org_id", None) or getattr(saloon, "org_id", None)
    breed = getattr(flock, "breed_type", None)
    order = ("-is_default", "template_id")
    # 2026-10-24 defect (live verification): lifecycle_templates is FORCE-RLS,
    # and the celery recache task (flock-created / assigned / nightly sweep)
    # runs WITHOUT a tenant GUC — the default alias saw zero templates and
    # every assignment silently fell through to the breed/default chain, so
    # assigned bands never reached the hash. Read through the same sanctioned
    # admin alias the saloon/flock reads in this module already use; the
    # org_id filters below keep the scoping.
    published = LifecycleTemplate.objects.using(_alias()).filter(
        status=LifecycleTemplate.PUBLISHED
    )

    assigned_id = _assigned_template_id(flock, saloon)
    if assigned_id is not None:
        row = published.filter(template_id=assigned_id, org_id=org_id).first()
        if row is not None:
            return row, SOURCE_ASSIGNED
        logger.warning(
            "saloon %s assignment %s is not a published own-org template — "
            "falling through to the breed/default chain",
            getattr(saloon, "pk", None) or getattr(flock, "saloon_id", None),
            assigned_id,
        )

    if breed:
        row = published.filter(org_id=org_id, breed_type=breed).order_by(*order).first()
        if row is not None:
            return row, SOURCE_ORG_BREED
    row = published.filter(org_id=org_id, is_default=True).order_by(*order).first()
    if row is not None:
        return row, SOURCE_ORG_DEFAULT
    row = published.filter(org__isnull=True, is_default=True).order_by(*order).first()
    if row is not None:
        return row, SOURCE_GLOBAL_DEFAULT
    return None, SOURCE_NONE


def _resolve_template(flock: FlockCycle, saloon=None):
    """Template only — the shape KPI/capabilities callers already expect
    (they pass the flock; the chain is the same code path either way)."""
    return effective_template(flock, saloon)[0]


def _alias() -> str:
    """RLS-bypassing read alias for cross-org threshold recache.

    The midnight backstop (recache_all_saloons) crosses orgs BY DESIGN but the
    worker connection is morghdari_app under FORCE-RLS — a default-alias
    Saloon.objects.all() sees ZERO rows and the cron silently no-ops (latent
    since M4). Same sanctioned admin-or-default pattern as
    monitoring.tasks._admin_alias and the M8-03 rebuild service.
    """
    from django.db import connections

    return "admin" if "admin" in connections.databases else "default"


def recache_saloon(saloon_id: int, clock: Clock | None = None) -> str:
    """Recompute + write one saloon's hash. Returns 'changed'|'unchanged'|'inert'."""
    client = _redis()
    saloon = Saloon.objects.using(_alias()).get(pk=saloon_id)
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
    for saloon in Saloon.objects.using(_alias()).all().iterator():
        if recache_saloon(saloon.pk, clock=clock) == "changed":
            changed += 1
    logger.info("daily threshold sweep: %s hashes changed", changed)
    return changed
