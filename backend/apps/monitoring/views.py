"""monitoring REST API — M6-01 (contract v1.5.0).

- GET /health/         → HealthCheckResponse (probes: database + redis).
- GET /hardware/time/  → {"unix_ts": …} NTP fallback, public (rate-limited at
  nginx, AU-fix 2026-08-16 — firmware keeps working when its RTC drifts).

The deeper gauges (queue_depth.{historical,pending_approval},
insert_buffer_length, subscriber.group_members, celery.active_workers) stay
null until their producers exist — a null is an honest \"not measured here\",
never a fabricated zero (D22/D23 degradation rule).

فارسی: وضعیتِ سلامتِ سرویس از دو کاوشگر (دیتابیس و ردیس) ساخته می‌شود؛ بقیهٔ
سنجه‌ها تا وقتی تولیدکننده‌شان نیامده null می‌مانند، نه صفرِ ساختگی.
"""

import os
import time

from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

APP_VERSION = "0.6"  # bumped by the M6-11 DoD sweep; contract v1.5.0 surface


def _redis():
    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True, socket_timeout=0.6)


def _db_probe() -> dict:
    started = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except Exception as exc:  # noqa: BLE001 — any probe failure is a status
        return {"status": "error", "latency_ms": None, "detail": str(exc)[:200]}


def _redis_probe() -> dict:
    started = time.perf_counter()
    try:
        client = _redis()
        client.ping()
        realtime = None
        historical = None
        try:
            realtime = client.xlen("telemetry:realtime")
        except Exception:  # noqa: BLE001
            realtime = None
        try:
            historical = client.xlen("telemetry:historical")
        except Exception:  # noqa: BLE001
            historical = None
        return {
            "status": "ok",
            "ping_ms": round((time.perf_counter() - started) * 1000, 2),
            "queue_depth": {
                "realtime": realtime,
                "historical": historical,
                "pending_approval": None,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "ping_ms": None, "detail": str(exc)[:200]}


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Contract HealthCheckResponse: healthy=200 / degraded=503.

    status is degraded when the DATABASE probe fails (app cannot serve) or the
    REDIS probe fails (the alert/telemetry stack cannot function) — matches
    the standing observability posture that a broker-less farm is sick, not
    fine. فارسی: اگر دیتابیس یا ردیس از کار بیفتند وضعیت degraded و کد ۵۰۳ است.
    """
    db = _db_probe()
    redis_out = _redis_probe()

    degraded = db["status"] != "ok" or redis_out["status"] != "ok"
    body = {
        "status": "degraded" if degraded else "healthy",
        "version": APP_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "services": {
            "database": db,
            "redis": {
                **redis_out,
                "memory_usage_mb": None,
                "insert_buffer_length": None,
            },
            "subscriber": {"group_members": None},
            "celery": {"status": None, "active_workers": None},
        },
    }
    return Response(body, status=status.HTTP_503_SERVICE_UNAVAILABLE if degraded else status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([AllowAny])
def hardware_time(request):
    """NTP fallback — current server time in whole unix seconds."""
    return Response({"unix_ts": int(time.time())})
