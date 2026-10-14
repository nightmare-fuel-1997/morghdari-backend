"""WebSocket docs views — human page + machine schema (docs-only).

Why this exists: drf-spectacular only introspects DRF HTTP views, while the
live sockets are Channels consumers (config/routing.py). Swagger therefore
covers REST only; frontend reads the WS protocol here instead of a faked
Swagger path whose "Try it out" could never open a socket.

Both views are public read-only (same posture as /api/docs/): a contract
you cannot see cannot be integrated against. Zero runtime effect on the
sockets themselves.
"""

from django.views.generic import TemplateView
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.schemas import TAG_TELEMETRY
from apps.telemetry import ws


class WsDocsView(TemplateView):
    """Human WS protocol page (Django template, not DRF — spectacular ignores it)."""

    template_name = "ws_docs.html"


class WsSchemaView(APIView):
    """Machine-readable WS schema (JSON) — examples built from ws.py builders."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=[TAG_TELEMETRY],
        operation_id="ws_schema",
        summary="WebSocket protocol schema (JSON)",
        description=(
            "Machine-readable WebSocket protocol: endpoints, auth, close codes "
            "and message examples. Examples are built from apps.telemetry.ws "
            "builders, so they cannot drift from the push code. "
            "See /api/ws-docs/ for the human page with JS/Dart snippets."
        ),
        auth=[],
        request=None,
        responses={200: {"type": "object"}},
    )
    def get(self, request):
        update_example = ws.telemetry_update_message(
            saloon_id=12,
            slot_id="GW-01",
            device_ts="2026-09-12T10:00:00Z",
            metrics={"temp": {"avg": 24.5}, "humidity": {"avg": 62.0}},
            is_buffered=False,
        )
        gap_example = ws.gap_fill_message(
            saloon_id=12,
            rows=[
                {
                    "slot_id": "GW-01",
                    "device_ts": "2026-09-12T10:00:00Z",
                    "server_received_ts": "2026-09-12T10:00:02Z",
                    "is_buffered": False,
                    "metrics": {"temp": {"avg": 24.5}},
                }
            ],
        )
        return Response(
            {
                "version": "1.0",
                "endpoints": [
                    {
                        "name": "saloon",
                        "path": "/ws/saloons/{saloon_id}/telemetry?token=<JWT>",
                        "auth": "JWT access token in ?token= query param (browsers cannot set WS headers).",
                        "description": "One socket per dashboard tab; every committed reading of that saloon is pushed.",
                    },
                    {
                        "name": "farm-compat",
                        "path": "/ws/farms/{farmSlug}/telemetry?token=<JWT>",
                        "canonical_twin": "/api/compat/v1/ws/farms/{farmSlug}/telemetry?token=<JWT>",
                        "description": "Legacy Flutter farm socket; subscribes to every saloon group of the farm, flat records, buffered rows never pushed.",
                    },
                ],
                "close_codes": {
                    "4003": "auth/org failure: missing/invalid token, no org claim, unknown or foreign saloon/farm, or no ACTIVE membership.",
                    "4000": "cap full: ws_max_connections_per_user slots already held (default 10).",
                },
                "client_messages": {
                    "gap_fill": {
                        "send": {"action": "gap_fill", "since": "<ISO datetime>", "slot_id": "<optional>"},
                        "reply": "telemetry.gap_fill",
                    },
                    "subscribe": {
                        "send": {"action": "subscribe"},
                        "note": "compat farm socket only; refreshes cap TTL, no other effect. Unknown inbound messages are ignored safely.",
                    },
                },
                "server_messages": {
                    "telemetry.hello": {
                        "when": "saloon socket connect",
                        "example": {
                            "type": "telemetry.hello",
                            "saloon_id": 12,
                            "org_id": "<org uuid>",
                            "thresholds": {},
                        },
                    },
                    "telemetry.rejoined": {
                        "when": "late group-join after a degraded handshake",
                        "example": {
                            "type": "telemetry.rejoined",
                            "saloon_id": 12,
                            "org_id": "<org uuid>",
                            "thresholds": {},
                        },
                    },
                    "telemetry.update": {
                        "when": "post-commit push of a durable reading; metrics verbatim from the stored row",
                        "example": update_example,
                    },
                    "telemetry.gap_fill": {
                        "when": "reply to gap_fill; rows ordered ascending, max 200 per reply",
                        "example": gap_example,
                    },
                    "connection_established": {
                        "when": "compat farm socket connect",
                        "example": {
                            "type": "connection_established",
                            "farm_slug": "farm-01",
                            "org_id": "<org uuid>",
                            "saloon_count": 2,
                        },
                    },
                },
                "human_docs": "/api/ws-docs/",
            }
        )
