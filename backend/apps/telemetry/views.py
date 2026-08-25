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

