"""Telemetry read API â€” GET /saloons/{id}/telemetry/ (contract v1.4.4).

RLS does the tenant scoping; this view only narrows time/slot/lane and shapes
the TelemetryReading contract (incl. the DB-GENERATED buffered_delay_mins read
via annotate â€” the column cannot live on the unmanaged model, see models.py).
ÙØ§Ø±Ø³ÛŒ: Ø®ÙˆØ§Ù†Ø¯Ù† ØªÙ„Ù‡â€ŒÙ…ØªØ±ÛŒ Ø¨Ø§ ÙÛŒÙ„ØªØ±Ù‡Ø§ÛŒ Ø²Ù…Ø§Ù†/Ø§Ø³Ù„Ø§ØªØ› Ø³ØªÙˆÙ†Ù ØªÙˆÙ„ÛŒØ¯Ø´Ø¯Ù‡ Ø¨Ø§ annotate
Ø®ÙˆØ§Ù†Ø¯Ù‡ Ù…ÛŒâ€ŒØ´ÙˆØ¯ Ùˆ RLS Ù…Ø±Ø²Ù Ø³Ø§Ø²Ù…Ø§Ù† Ø±Ø§ ØªØ¶Ù…ÛŒÙ† Ù…ÛŒâ€ŒÚ©Ù†Ø¯.
"""

from django.db.models.expressions import RawSQL
from django.utils.dateparse import parse_datetime

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.telemetry.models import SaloonTelemetry


def _bool_param(request, name: str, default: bool = False) -> bool:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_telemetry(request, saloon_id: int):
    queryset = SaloonTelemetry.objects.filter(saloon_id=saloon_id)

    include_buffered = _bool_param(request, "include_buffered", default=False)
    if not include_buffered:
        queryset = queryset.filter(is_buffered=False)

    since = request.query_params.get("since")
    until = request.query_params.get("until")
    from django.utils.dateparse import parse_datetime

    parsed_since = parse_datetime(since) if since else None
    parsed_until = parse_datetime(until) if until else None
    if parsed_since is not None:
        queryset = queryset.filter(device_ts__gte=parsed_since)
    if parsed_until is not None:
        queryset = queryset.filter(device_ts__lte=parsed_until)

    slot_id = request.query_params.get("slot_id")
    if slot_id:
        queryset = queryset.filter(slot_id=slot_id)

    # Generated column lives only in the DB â€” read it via SQL annotation.
    rows = queryset.order_by("device_ts").annotate(
        buffered_delay_mins=RawSQL("buffered_delay_mins", ())
    )
    results = []
    for row in rows:
        delay = getattr(row, "buffered_delay_mins", None)
        results.append({
            "server_received_ts": row.server_received_ts.isoformat(),
            "device_ts": row.device_ts.isoformat(),
            "is_buffered": bool(row.is_buffered),
            "buffered_delay_mins": float(delay) if delay is not None else None,
            "slot_id": row.slot_id,
            "metrics": row.metrics,
        })

    return Response(
        {"saloon_id": str(saloon_id), "results": results},
        status=status.HTTP_200_OK,
    )


# ---------------------------------------------------------------------------
# M6-01 (contract v1.5.0): analytics + metric catalog
# ---------------------------------------------------------------------------


def _parse_iso(value: str):
    return parse_datetime(value)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_analytics_hourly(request, saloon_id: int):
    """GET /saloons/{saloon_id}/analytics/hourly/ — hourly aggregates for charts.

    Reads metric_hourly_aggregates (built by the Celery aggregation task).
    from/to/metric are REQUIRED per contract; metric must exist in the
    sensor_metric_types catalog (400 otherwise — a typo is a typo, never an
    empty chart). With slot_id given the series is that slot's; without it the
    buckets are merged saloon-wide (sample_count-weighted avg, high_variance
    OR-ed per bucket) and slot_id echoes "" — one series, chart-ready.
    فارسی: نمودار ساعتی از جدولِ خلاصهٔ ساعتی؛ سنجه باید در کاتالوگ باشد وگرنه ۴۰۰.
    """
    from apps.telemetry.models import MetricHourlyAggregate, SensorMetricType

    from_iso = str(request.query_params.get("from", "")).strip()
    to_iso = str(request.query_params.get("to", "")).strip()
    metric = str(request.query_params.get("metric", "")).strip()
    if not (from_iso and to_iso and metric):
        return Response(
            {"error": "from, to and metric query parameters are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from_dt = _parse_iso(from_iso)
    to_dt = _parse_iso(to_iso)
    if from_dt is None or to_dt is None or from_dt >= to_dt:
        return Response(
            {"error": "from/to must be ISO datetimes with from < to"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not SensorMetricType.objects.filter(pk=metric).exists():
        return Response({"error": f"unknown metric_key {metric!r}"}, status=status.HTTP_400_BAD_REQUEST)

    slot_id = str(request.query_params.get("slot_id", "")).strip()
    qs = MetricHourlyAggregate.objects.filter(
        saloon_id=saloon_id, metric=metric, bucket_hr__gte=from_dt, bucket_hr__lte=to_dt
    )
    if slot_id:
        qs = qs.filter(slot_id=slot_id)

    rows = list(qs.values("bucket_hr", "slot_id", "avg_val", "sample_count", "high_variance").order_by("bucket_hr"))
    if slot_id:
        results = [
            {
                "bucket_hr": r["bucket_hr"].isoformat() if r["bucket_hr"] else None,
                "avg": float(r["avg_val"]) if r["avg_val"] is not None else None,
                "high_variance": bool(r["high_variance"]),
            }
            for r in rows
        ]
    else:
        merged: dict = {}
        for r in rows:
            bucket = merged.setdefault(
                r["bucket_hr"],
                {"bucket_hr": r["bucket_hr"], "sum": 0.0, "count": 0, "var": False},
            )
            sample = r["sample_count"] or 0
            bucket["sum"] += (r["avg_val"] or 0) * sample
            bucket["count"] += sample
            bucket["var"] = bucket["var"] or bool(r["high_variance"])
        results = [
            {
                "bucket_hr": b["bucket_hr"].isoformat() if b["bucket_hr"] else None,
                "avg": round(b["sum"] / b["count"], 4) if b["count"] else None,
                "high_variance": b["var"],
            }
            for b in sorted(merged.values(), key=lambda x: x["bucket_hr"] or from_dt)
        ]

    return Response({"metric": metric, "slot_id": slot_id, "results": results})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def metric_types(request):
    """GET /metric-types/ — the sensor catalog (dynamic frontend tiles).

    Any authenticated org member may read; rows are org-less global reference
    data (no RLS policy — model doc). is_tier1 drives the velocity lane.
    فارسی: کاتالوگ سنجه‌ها؛ دادهٔ سراسری بدون سازمان.
    """
    from apps.telemetry.models import SensorMetricType

    rows = SensorMetricType.objects.all().order_by("metric_key")
    return Response([
        {
            "metric_key": row.metric_key,
            "display_name": row.display_name,
            "unit": row.unit,
            "value_type": row.value_type or "numeric",
            "is_tier1": bool(row.is_tier1),
            "icon_name": row.icon_name,
        }
        for row in rows
    ])

