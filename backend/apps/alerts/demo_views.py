"""DEMO-ONLY alert-engine introspection — never exposed in production.

Mounted under /demo/api/ inside the SAME gate as the /demo/ page
(DEBUG or MORGHDARI_DEMO_UI=1). Requires an authenticated JWT.
Read-only over alert_instances/acknowledgements/mutes: the MET-ALERT-QUALITY
export (apps.alerts.services.quality) plus live Redis state-machine views
(breach keys, LOS fired-guards, mute counters) so the v0.4 showcase can show
the alert brain working without touching a terminal.
فارسی: نقاطِ دیدِ زندهٔ موتور هشدار برای دمو — فقط خواندن، با گیت و توکن معتبر.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.alerts.models import AlertInstance, MaintenanceMute
from apps.alerts.services.quality import (
    ack_to_resolve_seconds,
    alerts_per_flock,
    mttr_seconds_by_tier,
)


def _redis_client():
    """Same lazy client convention as sweeps/replay/mutes (tests inject)."""
    import os

    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def alert_quality(request):
    """MET-ALERT-QUALITY snapshot — the SAME functions the retrospective and
    (M7) Grafana consume. No reimplementation here."""
    return Response({
        "mttr_seconds_by_tier": mttr_seconds_by_tier(),
        "ack_to_resolve_seconds": ack_to_resolve_seconds(),
        "alerts_per_flock": alerts_per_flock(close_only=False),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def alert_overview(request):
    """Recent REALTIME/REPLAY alert rows + active maintenance mutes."""
    try:
        limit = max(1, min(int(request.query_params.get("limit", 20)), 100))
    except ValueError:
        limit = 20
    alerts = (
        AlertInstance.objects
        .order_by("-triggered_at")
        .values(
            "id", "saloon_id", "metric_type", "source", "current_status",
            "current_value", "highest_tier_reached", "triggered_at",
            "resolved_at",
        )[:limit]
    )
    mutes = (
        MaintenanceMute.objects
        .order_by("-starts_at")
        .values("id", "saloon_id", "reason", "starts_at", "ends_at")[:10]
    )
    return Response({
        "alerts": list(alerts),
        "maintenance_mutes": list(mutes),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def alert_state_machines(request):
    """Live Redis keys of the v0.4 state machines — breach accumulation,
    LOS fired-guards, correlation suppression, mute counters. Keys are
    SCANned by prefix; values are shown raw (small JSON/counters)."""
    client = _redis_client()
    families = {
        "breach": "alert:breach:*",
        "los_fired": "los:fired:*",
        "los_status": "sensor:status:*",
        "correlation_muted": "farm:corr:muted:*",
        "mute_count": "maintenance:mute:count:*",
        "replay_pending": "replay:pending:*",
    }
    out = {}
    for name, pattern in families.items():
        keys = []
        try:
            cursor = 0
            while len(keys) < 50:
                cursor, batch = client.scan(
                    cursor=cursor, match=pattern, count=100
                )
                keys.extend(batch)
                if cursor == 0:
                    break
        except Exception:
            pass
        rows = []
        for key in sorted(keys)[:50]:
            try:
                ktype = client.type(key)
                if ktype == "string":
                    value = client.get(key)
                    ttl = client.ttl(key)
                elif ktype == "zset":
                    value = client.zcard(key)
                    ttl = client.ttl(key)
                else:
                    value = f"<{ktype}>"
                    ttl = client.ttl(key)
                rows.append({"key": key, "type": ktype, "value": value, "ttl": ttl})
            except Exception:
                continue
        out[name] = {"count": len(keys), "keys": rows}
    return Response(out)
