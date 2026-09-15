"""GS-MAINT-MUTE — planned-maintenance suppression windows (M4-10).

While a mute window is active for a saloon, THRESHOLD-CROSSING and
DURATION-FILTER alerts are suppressed; readings still log (the telemetry
commit happens before evaluation) and VELOCITY emergencies ALWAYS bypass
(Decision 10 invariant — evaluation checks velocity first). One digest line
per ENDED window that suppressed alerts: the corpus (database.md) places it
in account_activity_logs, but that table's actor FK is NOT NULL and this is a
system-authored event — a schema decision (C-10 family) is needed. Until the
M4-12 retrospective rules on it, the digest is a structured WARNING log line
emitted by the escalation sweep, with suppression counts held in Redis per
Tracker §4i ("storage via config") — zero schema change.
فارسی: بی‌صداکنندهٔ تعمیرات — هشدارهای آستانه‌ای را در پنجرهٔ تعمیر خاموش می‌کند؛
سرعت هرگز؛ یک خطِ خلاصه به ازای هر پنجرهٔ تمام‌شده.
"""

import logging
from datetime import timedelta

from django.utils import timezone as dj_tz

from apps.alerts.models import MaintenanceMute
from apps.alerts.services.redis_keys import (
    maintenance_mute_count_key,
    maintenance_mute_digest_key,
)

logger = logging.getLogger(__name__)

DIGEST_LOOKBACK_DAYS = 2  # bounded scan for ended windows awaiting digest
DIGEST_MARKER_TTL_SEC = 7 * 86400


def _admin_alias() -> str:
    """RLS-bypassing read alias for cross-org discovery (monitoring pattern).

    BUG-9 family (QA 20.3a): the worker connects as morghdari_app under FORCE
    RLS — a default-alias MaintenanceMute read sees ZERO rows and the digest
    silently emits nothing. Discovery runs on the sanctioned admin alias (same
    as sweeps.escalate_all / replay.run_replay_sweep / thresholds.recache /
    rebuild_state / monitoring rollup); per-mute handling below is Redis-only
    (no DB writes), so no tenant_context is needed.
    فارسی: کشفِ میان‌سازمانی با اتصالِ ادمین؛ خروجیِ خلاصه فقط ردیس است و نیازی
    به زمینهٔ سازمان نیست.
    """
    from django.db import connections

    return "admin" if "admin" in connections.databases else "default"


def system_redis():
    """Same lazy client convention as sweeps/replay (tests inject)."""
    import os

    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True)


def active_maintenance_mute(saloon_id: int, now_dt) -> MaintenanceMute | None:
    """The mute window covering `now` for this saloon, if any."""
    return (
        MaintenanceMute.objects
        .filter(saloon_id=saloon_id, starts_at__lte=now_dt, ends_at__gt=now_dt)
        .order_by("-starts_at")
        .first()
    )


def note_suppressed(mute: MaintenanceMute, now_dt) -> None:
    """Count one suppressed alert for the window (feeds the digest line)."""
    client = system_redis()
    key = maintenance_mute_count_key(mute.id)
    ttl = max(int((mute.ends_at - now_dt).total_seconds()) + 60, 60)
    pipe = client.pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl)
    pipe.execute()


def write_maintenance_mute_digests(now_dt=None) -> list:
    """Escalation-sweep pass: ONE digest line per ended window that actually
    suppressed alerts (NX-guarded — never twice). Returns mute ids digested.
    فارسی: پنجره‌های تمام‌شدهٔ بی‌صداکننده یک‌بار خلاصه می‌شوند، نه بیشتر."""
    now_dt = now_dt or dj_tz.now()
    client = system_redis()
    digested = []
    # BUG-9 family (QA 20.3a): cross-org discovery on the admin alias — a bare
    # default-alias read sees zero rows as the app role under FORCE RLS.
    # فارسی: فهرستِ پنجره‌های تمام‌شده با ادمین؛ پردازشِ هر پنجره فقط ردیس است.
    ended = MaintenanceMute.objects.using(_admin_alias()).filter(
        ends_at__lte=now_dt,
        starts_at__gte=now_dt - timedelta(days=DIGEST_LOOKBACK_DAYS),
    ).iterator()
    for mute in ended:
        count_key = maintenance_mute_count_key(mute.id)
        raw = client.get(count_key)
        if not raw:
            continue
        if not client.set(
            maintenance_mute_digest_key(mute.id), "1",
            nx=True, ex=DIGEST_MARKER_TTL_SEC,
        ):
            continue
        logger.warning(
            "MAINTENANCE MUTE DIGEST: %s alerts suppressed by maintenance "
            "mute %s (saloon=%s reason='%s' window=%s..%s)",
            raw, mute.id, mute.saloon_id, mute.reason, mute.starts_at, mute.ends_at,
        )
        digested.append(mute.id)
        client.delete(count_key)
    return digested