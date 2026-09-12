"""Alert-subscription switching + suppression digest (v0.9 M9-10/B2).

Org-wide category mutes (one AlertSubscription row per muted metric_key).
The ENGINE consults muted_metrics() at evaluation lane ②c — AFTER the
velocity lane has already returned, so Tier-1 velocity emergencies
structurally cannot be muted (Decision 10 holds by construction, not by a
flag anyone can mis-set). LOS watchdog + escalation act on rows, never
categories, and are deliberately unwired here.

Digest (the D24-6 mitigation): every suppressed fire INCRs
submuted:{org}:{metric}:{tehran-day} (48 h TTL). The list endpoint serves
today's counts beside each mute — the audit trail of what silence cost,
without a row per suppressed reading.
"""

import datetime as dt
import logging

logger = logging.getLogger(__name__)

TEHRAN = dt.timezone(dt.timedelta(hours=3, minutes=30))  # fixed — DST abolished 2022


def system_redis():
    """Same lazy client convention as sweeps/mutes/replay (tests inject)."""
    import os

    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True)


def _today() -> dt.date:
    return dt.datetime.now(TEHRAN).date()


def _digest_key(org_id: str, metric_key: str, day: dt.date | None = None) -> str:
    return f"submuted:{org_id}:{metric_key}:{(day or _today()).isoformat()}"


def muted_metrics(org_id: str) -> set:
    """Metric keys muted org-wide (one cheap indexed read per evaluation)."""
    from apps.alerts.models import AlertSubscription

    return set(
        AlertSubscription.objects.filter(org_id=org_id).values_list("metric_key", flat=True)
    )


def note_sub_muted(client, org_id: str, metric_key: str) -> None:
    """Count one suppressed fire for the digest (never raises into evaluation)."""
    try:
        pipe = client.pipeline()
        pipe.incr(_digest_key(str(org_id), metric_key))
        pipe.expire(_digest_key(str(org_id), metric_key), 48 * 3600)
        pipe.execute()
    except Exception:  # noqa: BLE001 — digest loss must not break alerting
        logger.warning("sub-mute digest incr failed for %s/%s", org_id, metric_key)


def submuted_today(client, org_id: str) -> dict:
    """Today's suppression counts per metric (served beside the mute list)."""
    out = {}
    try:
        for key in client.keys(f"submuted:{org_id}:*:{_today().isoformat()}"):
            parts = key.split(":")
            # submuted:{org}:{metric}:{day} — four segments (org UUIDs carry no colons).
            if len(parts) == 4:
                out[parts[2]] = int(client.get(key) or 0)
    except Exception:  # noqa: BLE001 — same fail-soft contract
        logger.warning("sub-mute digest read failed for %s", org_id)
    return out
