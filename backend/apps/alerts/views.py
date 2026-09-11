"""alerts REST API — M6-01 v1.1 operability set (contract v1.5.0).

- GET  /saloons/{saloon_id}/alerts/   → {results: [AlertInstance]} (status/source filters)
- POST /alerts/{alert_id}/acknowledge/ → ack row + Redis pause key in one
  transaction (the M4-06 service; this is its REST surface, contract C-10:
  org_id comes from the JWT claim, never from client input).

RLS is the Postgres backstop; every queryset also carries the org filter so
sqlite loops behave identically. A cross-org list read is an EMPTY 200; a
cross-org acknowledge is a 404 (nothing to acknowledge).

فارسی: سطح REST هشدارها — فهرستِ هشدارهای یک سالن و رسیده‌سازیِ هشدار؛ رسیده‌سازی
ردیف و کلیدِ ردیس را یک‌جا می‌نویسد.
"""

from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.alerts.models import AlertInstance
from apps.alerts.serializers import (
    ALERT_ACK_REQUEST_EXAMPLE,
    ALERT_ACK_RESPONSE_EXAMPLE,
    ALERT_LIST_RESPONSE_EXAMPLE,
    AlertAcknowledgeRequestSerializer,
    AlertAcknowledgeResponseSerializer,
    SaloonAlertsResponseSerializer,
)
from apps.common.schemas import TAG_ALERTS, error_responses
from apps.alerts.services.ack import acknowledge_alert as acknowledge_service
from apps.alerts.services.ack import pause_minutes

# Escalation-aware humans who may pause a Tier-1 (contract: worker/engineer —
# managers/owners are escalation recipients and may ack too; the role gate is
# "someone who works the farm floor or above", never a bare reader).
ACK_ROLES = {"worker", "engineer", "manager", "owner"}


def _org_id(request):
    return _claims(request).get("org_id")


def _alert_out(row: AlertInstance) -> dict:
    return {
        "alert_id": str(row.id),
        "metric_type": row.metric_type,
        "current_status": row.current_status,
        "current_value": float(row.current_value) if row.current_value is not None else None,
        "source": row.source,
        "triggered_at": row.triggered_at.isoformat() if row.triggered_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "highest_tier_reached": row.highest_tier_reached,
    }


# ---------------------------------------------------------------------------
# GET /saloons/{saloon_id}/alerts/
# ---------------------------------------------------------------------------
@extend_schema(
    tags=[TAG_ALERTS],
    operation_id="list_saloon_alerts",
    summary="List alerts for a saloon",
    description="""Active alerts (default) or the full history for one saloon.
Newest-first by `triggered_at`; rows are RLS-scoped to the caller's org.

Filters: `status=ACTIVE` (default, `resolved_at IS NULL`) or `status=ALL`;
`source=REALTIME|REPLAY_ANALYSIS` narrows the lane. Anything else is a 400 —
a typo must never silently widen a page.

A foreign/unknown `saloon_id` is an **empty 200**, never a 404 (C-14: ids
outside the caller's org are never distinguished from unknown ids).""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        ),
        OpenApiParameter(
            "status",
            str,
            OpenApiParameter.QUERY,
            required=False,
            enum=["ACTIVE", "ALL"],
            description="ACTIVE (default) skips resolved rows; ALL returns the history.",
        ),
        OpenApiParameter(
            "source",
            str,
            OpenApiParameter.QUERY,
            required=False,
            enum=["REALTIME", "REPLAY_ANALYSIS"],
            description="Restrict to one alert source lane.",
        ),
    ],
    request=None,
    responses={
        200: SaloonAlertsResponseSerializer,
        **error_responses(400, 401),
    },
    examples=[ALERT_LIST_RESPONSE_EXAMPLE],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_alerts(request, saloon_id: int):
    org_id = _org_id(request)
    qs = AlertInstance.objects.filter(saloon_id=saloon_id)
    if org_id:
        qs = qs.filter(org_id=org_id)

    wanted = str(request.query_params.get("status", "ACTIVE")).strip().upper()
    if wanted == "ACTIVE":
        qs = qs.filter(resolved_at__isnull=True)
    elif wanted not in ("ALL", ""):
        return Response(
            {"error": "status must be ACTIVE or ALL"}, status=status.HTTP_400_BAD_REQUEST
        )

    source = str(request.query_params.get("source", "")).strip().upper()
    if source:
        if source not in ("REALTIME", "REPLAY_ANALYSIS"):
            return Response(
                {"error": "source must be REALTIME or REPLAY_ANALYSIS"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = qs.filter(source=source)

    rows = qs.order_by("-triggered_at")
    return Response({"results": [_alert_out(r) for r in rows]})


# ---------------------------------------------------------------------------
# POST /alerts/{alert_id}/acknowledge/
# ---------------------------------------------------------------------------
@extend_schema(
    tags=[TAG_ALERTS],
    operation_id="acknowledge_alert",
    summary="Acknowledge an alert",
    description="""Writes the acknowledgement row and the Redis escalation-pause key
in one transaction (M4-06 service). Allowed for `worker`/`engineer`/`manager`/
`owner` — a bare reader cannot pause an escalation.

Acknowledging pauses **escalation** for the configured window (default
10 minutes, `system_configuration`); it does not resolve the alert or hide a
Tier-1 velocity breach from the paging paths (Decision 10: NH3 and temp
velocity always page).

Cross-org ids are a 404 — nothing to acknowledge, never an oracle.""",
    parameters=[
        OpenApiParameter(
            "alert_id",
            str,
            OpenApiParameter.PATH,
            description="Alert instance id (uuid).",
        )
    ],
    request=AlertAcknowledgeRequestSerializer,
    responses={
        200: AlertAcknowledgeResponseSerializer,
        **error_responses(401, 403, 404),
    },
    examples=[ALERT_ACK_REQUEST_EXAMPLE, ALERT_ACK_RESPONSE_EXAMPLE],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def alert_acknowledge(request, alert_id):
    org_id = _org_id(request)
    if not org_id or not (set(_claims(request).get("roles", [])) & ACK_ROLES):
        return Response({"error": "assigned worker/engineer required"}, status=403)

    alert = AlertInstance.objects.filter(org_id=org_id, id=alert_id).first()
    if alert is None:
        return Response({"error": "alert not found"}, status=404)

    notes = str(request.data.get("notes", "")).strip() or None
    row = acknowledge_service(alert, request.user, notes=notes)
    return Response(
        {
            "alert_id": str(alert.id),
            "acknowledged_by": request.user.id,
            "pause_minutes": row.pause_minutes or pause_minutes(),
            "status": "ACKNOWLEDGED",
        },
        status=status.HTTP_200_OK,
    )
