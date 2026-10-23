"""Shared OpenAPI components — reusable pieces for every @extend_schema.

Docs-only: plain DRF serializers + OpenApiResponse constants. No model,
no view, no migration. Every error shape below mirrors the envelopes the
views actually return ({"error": ...} with optional detail), so the
generated schema matches backend/openapi.yaml instead of inventing shapes.

Usage per view::

    from apps.common.schemas import TAG_FARMS, error_responses, ErrorEnvelopeSerializer

    @extend_schema(
        tags=[TAG_FARMS],
        summary="...",
        description="...",
        parameters=[...],
        request=SomeSerializer,          # or None for GET/DELETE
        responses={200: SomeSerializer, **error_responses(400, 401, 403, 404)},
    )

``error_responses(*codes)`` exists so each view documents ONLY the verdicts it
can actually return — a list endpoint that answers a foreign id with an empty
200 must not advertise a 404 it never sends (the M6-01 empty-200 rule).
"""

from drf_spectacular.utils import OpenApiResponse
from rest_framework import serializers


class ErrorEnvelopeSerializer(serializers.Serializer):
    """Standard error envelope: {"error": "<code>", "detail": "<human>"}."""

    error = serializers.CharField(
        help_text="Stable machine-readable error code, e.g. forbidden, not found."
    )
    detail = serializers.CharField(
        required=False,
        help_text="Human-readable explanation. Omitted on terse 403/404 paths.",
    )


class ValidationErrorSerializer(serializers.Serializer):
    """DRF 400 body: per-field message lists plus optional non-field errors."""

    detail = serializers.CharField(
        required=False, help_text="Top-level message when the error is not field-specific."
    )


class PaginatedListSerializer(serializers.Serializer):
    """Note only — views in this codebase return bare JSON lists (no page
    envelope), so this exists to SAY so explicitly in one place. Do not wrap
    list responses in it; reference it in descriptions instead."""

    count = serializers.IntegerField(help_text="Total rows matching the query.")
    results = serializers.ListField(
        child=serializers.DictField(), help_text="The page of rows (bare objects)."
    )


ERROR_RESPONSES = {
    400: OpenApiResponse(
        response=ErrorEnvelopeSerializer,
        description="Validation error — malformed input, bad enum, or out-of-range value.",
    ),
    401: OpenApiResponse(
        response=ErrorEnvelopeSerializer,
        description="Unauthenticated — missing, expired, or malformed Bearer token.",
    ),
    403: OpenApiResponse(
        response=ErrorEnvelopeSerializer,
        description="Forbidden — valid token but the role claim lacks this surface.",
    ),
    404: OpenApiResponse(
        response=ErrorEnvelopeSerializer,
        description="Not found — id/slug unknown OR outside the caller's org (never distinguished).",
    ),
}

def error_responses(*codes: int) -> dict:
    """Subset of ERROR_RESPONSES for one view's real verdict set.

    Keeps the docs honest per the C-14 empty-200 convention: only spread the
    codes the view body can return.
    """
    return {code: ERROR_RESPONSES[code] for code in codes}


BAD_REQUEST_RESPONSE = OpenApiResponse(
    response=ErrorEnvelopeSerializer,
    description="Bad request — the body failed validation (unknown kind, missing field).",
)

CONFLICT_RESPONSE = OpenApiResponse(
    response=ErrorEnvelopeSerializer,
    description="Conflict — state precondition failed (e.g. concurrent sync lock, wrong flock state).",
)

RATE_LIMITED_RESPONSE = OpenApiResponse(
    response=ErrorEnvelopeSerializer,
    description="Rate limited — strict lane (auth_strict 3/min) exceeded. Retry after 60s.",
)

# Tags — MUST match the TAGS list in SPECTACULAR_SETTINGS (config/settings/base.py).
TAG_AUTH = "Authentication"
TAG_ORG = "Org Management"
TAG_FARMS = "Farms & Saloons"
TAG_TELEMETRY = "Telemetry"
TAG_PROVISIONING = "Provisioning & Gateways"
TAG_ALERTS = "Alerts & KPI"
TAG_MONITORING = "Monitoring"
TAG_OPS = "Ops"
TAG_REPORTS = "Reports"
TAG_SUPPORT = "Platform Support"
TAG_COMPAT = "Compat"
