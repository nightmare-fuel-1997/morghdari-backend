"""T3 drill — rebuild Redis alert state from DB truth (M8-03, DR §3.2).

After a catastrophic Redis wipe the durable ledger (Postgres) still knows
which alerts were OPEN; the rebuild re-materialises the two key families the
alert engine lives on:

  * breach keys       `saloon:{id}:breach:{metric}` — JSON {started_at,
                       alerted, value} with a fresh breach_duration_sec TTL;
  * active pointers   `alert:active:saloon:{id}:{metric}` — the coalesce
                       blob (org/farm/saloon/metric/current_tier/alert_id)
                       the sweeps resolve against.

Postgres is the truth; this only re-creates Redis state from it and is
idempotent (SET overwrites converge on the second run). Rows that never had
Redis state are deliberately NOT re-created:

  * metric_type='los' — LOS alerts fire from live `sensor:status` keys, never
    from a pre-existing breach/active key (the LOS watchdog owns that path,
    and the post-restart suppression window covers the void);
  * source='REPLAY_ANALYSIS' — historical labels, never pushed (DEMO §2).

Cross-org reads route through the `admin` DATABASES alias — the SAME
sanctioned pattern as monitoring.build_org_usage (the worker connection is
morghdari_app under FORCE-RLS; the admin role is the documented BYPASSRLS
channel). CI/laptop leave the alias unset → `default` (superuser there).

فارسی: بازسازیِ وضعیتِ فعالِ هشدارها از دفترِ دیتابیس پس از پاک‌شدنِ ردیس —
کلیدهای نقض و اشاره‌گرِ هشدارِ فعال با همان شکلِ تولید بازساخته می‌شوند.
"""

import json
import logging

from django.db import connections

from apps.alerts.models import AlertInstance
from apps.alerts.services.redis_keys import active_alert_key, breach_key

logger = logging.getLogger(__name__)

INFO_METRIC = "metric_type"


def _admin_alias() -> str:
    """RLS-bypassing connection for cross-org sweeps (monitoring pattern).

    `admin` when base.py built it from DATABASE_ADMIN_URL (prod worker);
    otherwise `default` — in CI/laptop the connect user is the DB superuser
    and already bypasses RLS. sqlite: no aliases, no RLS → `default`.
    """
    return "admin" if "admin" in connections.databases else "default"


def _config_int(key: str, default: int) -> int:
    from apps.system_config.models import SystemConfiguration

    raw = (
        SystemConfiguration.objects.using(_admin_alias())
        .filter(key=key)
        .values_list("value", flat=True)
        .first()
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _system_redis():
    """Redis connection (same URL family as sweeps/consumers)."""
    import os

    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True)


def rebuild_active_state(client=None) -> dict:
    """Re-create breach + active-alert keys for every OPEN realtime alert.

    Runs inside the T3 drill right after the Redis volume comes back, BEFORE
    the threshold cache warmers (order per DR §3.2). Idempotent.
    """
    client = client or _system_redis()

    alias = _admin_alias()
    open_rows = list(
        AlertInstance.objects.using(alias)
        .filter(resolved_at__isnull=True, source="REALTIME")
        .exclude(metric_type="los")
    )
    los_rows = list(
        AlertInstance.objects.using(alias)
        .filter(resolved_at__isnull=True, source="REALTIME", metric_type="los")
        .values_list("id", flat=True)
    )
    replay_count = (
        AlertInstance.objects.using(alias)
        .filter(resolved_at__isnull=True, source="REPLAY_ANALYSIS")
        .count()
    )

    duration_sec = _config_int("breach_duration_sec", 900)

    counts = {"breach_keys": 0, "active_keys": 0, "skipped_los": len(los_rows),
              "skipped_replay": replay_count}

    pipe = client.pipeline()
    for row in open_rows:
        triggered = row.triggered_at.timestamp() if row.triggered_at else 0.0
        value = float(row.current_value or 0)

        b_key = breach_key(row.saloon_id, row.metric_type)
        pipe.setex(
            b_key,
            duration_sec,
            json.dumps({"started_at": int(triggered), "alerted": True, "value": value}),
        )
        counts["breach_keys"] += 1

        a_key = active_alert_key(row.saloon_id, row.metric_type)
        blob = json.dumps({
            "alert_id": str(row.id),
            "org_id": str(row.org_id),
            "farm_id": row.farm_id,
            "saloon_id": row.saloon_id,
            "metric": row.metric_type,
            "current_tier": row.highest_tier_reached or 1,
            "reason": "REBUILD_AFTER_REDIS_WIPE",
        })
        pipe.set(a_key, blob)
        counts["active_keys"] += 1

    pipe.execute()
    logger.info(
        "rebuild_active_state: %s breach keys, %s active keys "
        "(skipped %s LOS, %s REPLAY_ANALYSIS)",
        counts["breach_keys"], counts["active_keys"],
        counts["skipped_los"], counts["skipped_replay"],
    )
    return counts