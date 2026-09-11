"""compat serializers — docs-only DRF shapes for @extend_schema (openapi-annotations).

W1..W5 are thin shims over the canonical merge surface: response shapes are
the canonical reports shapes (subclassed here so the Compat tag owns explicit
component names), while the W3 write body adds the two slug keys the shim
resolves via legacy_slug_map. Reporter is always the JWT user — no
reporter/reported_by key exists on any write body (client-sent ones are
ignored). The existing serializers_compat.py holds read-only dict builders
and is intentionally NOT refactored — these are separate @extend_schema
components only.
"""

from apps.reports.serializers import (
    ReportCreateSerializer,
    ReportEditSerializer,
    ReportListItemSerializer,
    ReportSingleSerializer,
)
from rest_framework import serializers


class CompatReportListItemSerializer(ReportListItemSerializer):
    """W1/W5 list item — byte-identical to the canonical merged list item."""

    pass


class CompatReportSingleSerializer(ReportSingleSerializer):
    """W2 single object — byte-identical to the canonical merged single."""

    pass


class CompatReportCreateSerializer(ReportCreateSerializer):
    """W3 write body — every §2 canonical/alias field plus the two slug keys.

    ``salon_slug`` keeps the Flutter single-l spelling; both slugs are
    required and must agree (the salon's farm == the farm — a mismatch is a
    404, never a 400 oracle)."""

    farm_slug = serializers.CharField(help_text="Legacy farm slug (legacy_slug_map).")
    salon_slug = serializers.CharField(help_text="Legacy salon slug, Flutter spelling (legacy_slug_map).")


class CompatReportEditSerializer(ReportEditSerializer):
    """W4 edit body — same canonical + alias fields as the canonical PATCH."""

    pass
