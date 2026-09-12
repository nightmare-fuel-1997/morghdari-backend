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
from django.http import HttpResponse
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, renderer_classes
from rest_framework.permissions import AllowAny
from rest_framework.renderers import BrowsableAPIRenderer, JSONRenderer
from rest_framework.response import Response

from apps.common.schemas import TAG_MONITORING
from apps.monitoring.serializers import (
    HardwareTimeSerializer,
    HealthResponseSerializer,
    StatusPageSerializer,
)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
STATUS_SLO_PCT = 99.9
STATUS_WINDOW = "14d"


def _prom_query(expr: str, timeout: float = 5.0) -> dict:
    """Minimal Prometheus HTTP API client (stdlib — no new dependency)."""
    import json
    import urllib.parse
    import urllib.request

    url = PROMETHEUS_URL.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _prober_uptime() -> tuple[str, float | None, list]:
    """14-day probe_success share per blackbox vantage (MET-STATUS).

    overall = MINIMUM across vantages (fail-closed reporting: the worst
    vantage is the headline). Returns (status, overall_pct, probers).
    Raises on Prometheus failure — the caller turns that into `unknown`,
    never a 500 (a status page must not die with its monitor).
    """
    data = _prom_query(
        'avg_over_time(probe_success{job=~"blackbox-external.*"}[%s])' % STATUS_WINDOW
    )
    probers = []
    for series in data.get("data", {}).get("result", []):
        metric = series.get("metric", {})
        value = series.get("value", [None, None])[1]
        pct = round(float(value) * 100.0, 3) if value not in (None, "NaN") else None
        probers.append({
            "job": metric.get("job", "?"),
            "target": metric.get("instance", metric.get("target", "?")),
            "uptime_pct": pct,
        })
    probers.sort(key=lambda p: p["job"])
    measured = [p["uptime_pct"] for p in probers if p["uptime_pct"] is not None]
    if not measured:
        return "unknown", None, probers
    overall = min(measured)
    return ("operational" if overall >= STATUS_SLO_PCT else "degraded"), overall, probers

APP_VERSION = "0.8-DR"  # bumped past the v0.8-DR close (was 0.7)


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


@extend_schema(
    tags=[TAG_MONITORING],
    operation_id="health_check",
    summary="Service health probe",
    description="""Public, unauthenticated health check (load balancers and the
monitoring stack call it): database + Redis probes.

`healthy` → 200; `degraded` → **503** when the DATABASE probe fails (the app
cannot serve) or the REDIS probe fails (the alert/telemetry stack cannot
function — a broker-less farm is sick, not fine).

Gauges whose producers do not exist yet stay `null` — an honest "not measured
here", never a fabricated zero (D22/D23 degradation rule):
`redis.queue_depth.pending_approval`, `redis.memory_usage_mb`,
`redis.insert_buffer_length`, `subscriber.group_members`, `celery.*`.""",
    auth=[],
    request=None,
    responses={
        200: HealthResponseSerializer,
        503: OpenApiResponse(
            response=HealthResponseSerializer,
            description="Degraded — the database or Redis probe failed.",
        ),
    },
    examples=[
        OpenApiExample(
            "Healthy",
            response_only=True,
            value={
                "status": "healthy",
                "version": APP_VERSION,
                "timestamp": "2026-09-06T12:00:00Z",
                "services": {
                    "database": {"status": "ok", "latency_ms": 0.42},
                    "redis": {
                        "status": "ok",
                        "ping_ms": 0.31,
                        "queue_depth": {
                            "realtime": 0,
                            "historical": 12,
                            "pending_approval": None,
                        },
                        "memory_usage_mb": None,
                        "insert_buffer_length": None,
                    },
                    "subscriber": {"group_members": None},
                    "celery": {"status": None, "active_workers": None},
                },
            },
        )
    ],
)
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


@extend_schema(
    tags=[TAG_MONITORING],
    operation_id="hardware_time",
    summary="Server time for hardware NTP fallback",
    description="""Public, unauthenticated: current server time in whole unix seconds, so
device firmware keeps working when its RTC drifts (AU-fix 2026-08-16).
Rate-limited at nginx; not a DRF-authenticated surface.""",
    auth=[],
    request=None,
    responses={200: HardwareTimeSerializer},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def hardware_time(request):
    """NTP fallback - current server time in whole unix seconds."""
    return Response({"unix_ts": int(time.time())})


@extend_schema(
    tags=[TAG_MONITORING],
    operation_id="public_status_page",
    summary="Public status page: 14-day uptime from probe history (v0.9/B4)",
    description="""Unauthenticated (MET-STATUS). Renders blackbox probe_success
history only — aggregate booleans, zero tenant data by construction (nothing
org-scoped is touched). `Accept: text/html` returns the browsable human page.
Prometheus unreachable → 200 `unknown`, never a 500.""",
    responses={200: StatusPageSerializer},
)
@api_view(["GET"])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer, BrowsableAPIRenderer])
def status_page(request):
    """GET /status/ — the pilot's public uptime page.

    JSON by default; text/html renders DRF's browsable page (human-readable,
    zero templates — the pilot needs a readable page, not a designed one).
    """
    import datetime as dt

    try:
        status, overall, probers = _prober_uptime()
    except Exception:  # noqa: BLE001 — unknown, not 500 (see docstring)
        status, overall, probers = "unknown", None, []
    return Response({
        "status": status,
        "uptime_14d_pct": overall,
        "probers": probers,
        "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def metrics(request):
    """Prometheus scrape endpoint — Observability Runbook §3.1 (v0.7 M7-04).

    NOT a DRF @api_view: the exposition format is plain text, not JSON, and
    DRF's content negotiation would 400 a plain-GET scrape (the exact failure
    the monitoring rehearsal caught). Scrape-time refresh_gauges() drains the
    Redis bridge so worker-side counters/latencies surface here.

    Contract: documented in openapi.yaml (the implemented == contract gate
    walks every /api/v1/ route), but nginx returns 404 for it — the scrape
    targets daphne directly on the internal network (Deployment Guide §7).
    """
    from apps.monitoring.metrics import refresh_gauges

    refresh_gauges()
    return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)


# The contract gate's _view_methods() detects served verbs via attached
# attributes (DRF's @api_view does exactly this under the hood) — a bare
# function is invisible to it, so mirror the DRF shape explicitly.
metrics.get = metrics
