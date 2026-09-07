"""Telemetry read API — saloon + farm telemetry reads (contract v1.5.0 + M7-12).

- GET /saloons/{saloon_id}/telemetry/ — gap-fill or historical readings for ONE
  saloon (contract v1.4.4 shape, byte-stable). ?limit=/?offset= (M7-12) activate
  NEWEST-FIRST paging (?limit=1 IS the latest reading — the Flutter chart calls
  ?limit=24); without them the list stays chronological ascending, byte-identical
  to the v0.6 behavior.
- GET /farms/{farm_id}/telemetry/ (M7-12, v1.6 native) — the farm's saloon
  streams merged into ONE TelemetryReading list (rows gain saloon_id); the farm
  list equals the union of the per-saloon lists for the same window.

RLS does the tenant scoping in production; every queryset ALSO carries the
claim org explicitly (defense-in-depth, the M6-01 convention — sqlite loops
behave identically). The DB-GENERATED buffered_delay_mins column is read via
annotate (it cannot live on the unmanaged model, see models.py).
فارسی: خواندن تله‌متری سالن و مزرعه با فیلترهای زمان/اسلات؛ صفحه‌بندی از نو به
قدیم است تا ?limit=1 همان آخرین خواندن باشد؛ ستون تولیدشده با annotate خوانده
می‌شود و RLS مرز سازمان را تضمین می‌کند.
"""

from django.db.models.expressions import RawSQL
from django.utils.dateparse import parse_datetime

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.farms.models import Farm
from apps.telemetry.models import SaloonTelemetry

# The paging ceiling — same as the compat history windows (resolve_history_window).
PAGING_MAX_LIMIT = 1000


def _org_id(request):
    return _claims(request).get("org_id")


def _bool_param(request, name: str, default: bool = False) -> bool:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def _parse_paging(request):
    """(error, (limit, offset)) — paging mode when either ?limit=/?offset= given.

    Paging mode returns rows NEWEST-FIRST so ?limit=1 is THE latest reading;
    legacy no-param calls stay chronological ascending (byte-compat with v0.6).
    limit alone pages from the top; offset alone pages a full-history window.
    """
    raw_limit = str(request.query_params.get("limit", "")).strip()
    raw_offset = str(request.query_params.get("offset", "")).strip()
    if not raw_limit and not raw_offset:
        return None, None

    limit = None
    if raw_limit:
        try:
            limit = int(raw_limit)
        except ValueError:
            return Response({"error": "limit must be an integer"}, status=400), None
        if not 1 <= limit <= PAGING_MAX_LIMIT:
            return (
                Response(
                    {"error": f"limit must be between 1 and {PAGING_MAX_LIMIT}"},
                    status=400,
                ),
                None,
            )

    offset = 0
    if raw_offset:
        try:
            offset = int(raw_offset)
        except ValueError:
            return Response({"error": "offset must be an integer"}, status=400), None
        if offset < 0:
            return Response({"error": "offset must be >= 0"}, status=400), None

    return None, (limit, offset)


def _windowed_queryset(request, queryset):
    """The shared since/until/slot_id/include_buffered narrowing (v1.4.4
    semantics byte-preserved: an unparseable since/until is IGNORED, not a 400)."""
    include_buffered = _bool_param(request, "include_buffered", default=False)
    if not include_buffered:
        queryset = queryset.filter(is_buffered=False)

    since = request.query_params.get("since")
    until = request.query_params.get("until")
    parsed_since = parse_datetime(since) if since else None
    parsed_until = parse_datetime(until) if until else None
    if parsed_since is not None:
        queryset = queryset.filter(device_ts__gte=parsed_since)
    if parsed_until is not None:
        queryset = queryset.filter(device_ts__lte=parsed_until)

    slot_id = request.query_params.get("slot_id")
    if slot_id:
        queryset = queryset.filter(slot_id=slot_id)
    return queryset


def _reading_payload(row, saloon_id=None) -> dict:
    """One TelemetryReading body (contract v1.4.4); farm rows add saloon_id."""
    delay = getattr(row, "buffered_delay_mins", None)
    payload = {
        "server_received_ts": row.server_received_ts.isoformat(),
        "device_ts": row.device_ts.isoformat(),
        "is_buffered": bool(row.is_buffered),
        "buffered_delay_mins": float(delay) if delay is not None else None,
        "slot_id": row.slot_id,
        "metrics": row.metrics,
    }
    if saloon_id is not None:
        payload["saloon_id"] = str(saloon_id)
    return payload


def _paged_rows(request, queryset):
    """(error, rows) — shared ordering + limit/offset slicing for both reads."""
    error, paging = _parse_paging(request)
    if error is not None:
        return error, None

    # Generated column lives only in the DB — read it via SQL annotation.
    annotated = queryset.annotate(buffered_delay_mins=RawSQL("buffered_delay_mins", ()))
    if paging is not None:
        limit, offset = paging
        rows = annotated.order_by("-device_ts", "-server_received_ts")[
            offset : (offset + limit) if limit is not None else None
        ]
    else:
        rows = annotated.order_by("device_ts")
    return None, rows


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_telemetry(request, saloon_id: int):
    org_id = _org_id(request)
    if org_id is None:
        return Response({"error": "organization context required"}, status=400)

    queryset = SaloonTelemetry.objects.filter(org_id=org_id, saloon_id=saloon_id)
    queryset = _windowed_queryset(request, queryset)

    error, rows = _paged_rows(request, queryset)
    if error is not None:
        return error

    results = [_reading_payload(row) for row in rows]
    return Response(
        {"saloon_id": str(saloon_id), "results": results},
        status=status.HTTP_200_OK,
    )


# ---------------------------------------------------------------------------
# M7-12 (v1.6 native): farm-wide telemetry merge
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farm_telemetry(request, farm_id: int):
    """GET /farms/{farm_id}/telemetry/ — the farm's saloon streams, merged.

    One TelemetryReading list across ALL the farm's saloons (rows gain
    saloon_id); the acceptance contract is that this list equals the UNION of
    the per-saloon lists for the same window. Foreign/unknown farm → 404
    (single-resource read, same verdict as /farms/{farm_id}/overview/).
    فارسی: تله‌متری کل مزرعه — ادغام جریان سالن‌ها؛ هر ردیف saloon_id خودش را دارد.
    """
    org_id = _org_id(request)
    if org_id is None:
        return Response({"error": "organization context required"}, status=400)

    farm = Farm.objects.filter(org_id=org_id, pk=farm_id).first()
    if farm is None:
        return Response({"error": "farm not found"}, status=status.HTTP_404_NOT_FOUND)

    queryset = SaloonTelemetry.objects.filter(org_id=org_id, saloon__farm_id=farm.pk)
    queryset = _windowed_queryset(request, queryset)

    error, rows = _paged_rows(request, queryset)
    if error is not None:
        return error

    results = [_reading_payload(row, saloon_id=row.saloon_id) for row in rows]
    return Response(
        {"farm_id": farm.pk, "results": results},
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
    فارسی: نمودار ساعتی از جدول خلاصهٔ ساعتی؛ سنجه باید در کاتالوگ باشد وگرنه ۴۰۰.
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
