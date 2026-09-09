"""Compat REST views — W1..W5 report aliases (M7-17, docs/api-reports-merge-v1.md §5:
thin shims over the canonical merge surface — slugs via legacy_slug_map,
reporter is always the JWT user, never client input).

M7-14 scoped removal: the C1..C15 REST handlers (orgs/farms/devices/telemetry)
are deleted; only the W1..W5 entry points remain. Shared helpers used by W
(_org_claim) are kept; C-only helpers (_org_slug_row, _device_items, history
windowing, _farm_saloon_ids) are deleted with their views. services_compat.py,
serializers_compat.py, models.py (legacy_slug_map + backfill) and
consumers_compat.py are untouched.

Thin by design: auth (DRF IsAuthenticated; the tenant middleware already
verified the Bearer token and armed the GUC) → compat_resolve_* (charset +
org-claim check; a miss is ALWAYS 404 with the spec's error strings, never
500, never an empty 200).

Global error contract: {"error": "<string>"} with the spec §8 codes —
404 unknown slug · 400 inverted window / bad charset.

فارسی: ویوهای نازلِ سازگاری — هر خطای اسلاگِ خارجی یا بد، ۴۰۴ تمیز است.
"""

from datetime import datetime as _datetime

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.compat import services_compat as svc
from apps.reports import api as reports_api
from apps.reports.models import DailyReport


def _org_claim(request):
    return _claims(request).get("org_id")


# ---------------------------------------------------------------------------
# Reports (W1..W5) — thin shims over the canonical merge surface (M7-17,
# docs/api-reports-merge-v1.md §5). Slugs resolve via legacy_slug_map
# (compat_resolve_farm_saloon: either miss or farm/saloon disagreement is
# 404, never a 400 oracle); reporter is ALWAYS the JWT user — the canonical
# views set submitted_by=request.user and never read a client-sent
# reporter/reported_by key. Canonical list shape is the bare merged list
# (no {results,count} envelope — spec wins).
# ---------------------------------------------------------------------------


def _report_window_or_400(request):
    """from/to are calendar dates (YYYY-MM-DD). Unparseable → 400;
    from > to → 400 invalid time window (compat §8, same string as the
    telemetry history windowing). Returns ((from, to), None) on success,
    ((None, None), error_response) on failure."""
    raw_from = request.query_params.get("from")
    raw_to = request.query_params.get("to")
    try:
        date_from = _datetime.strptime(raw_from, "%Y-%m-%d").date() if raw_from else None
        date_to = _datetime.strptime(raw_to, "%Y-%m-%d").date() if raw_to else None
    except (TypeError, ValueError):
        return (None, None), Response(
            {"error": "from/to must be YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST
        )
    if date_from is not None and date_to is not None and date_from > date_to:
        return (None, None), Response(
            {"error": "invalid time window"}, status=status.HTTP_400_BAD_REQUEST
        )
    return (date_from, date_to), None


def _merged_report_qs(**filters):
    """Canonical list queryset shape (reports.api saloon_reports verbatim):
    merged builder, newest-first, extras prefetched."""
    return (
        DailyReport.objects.filter(**filters)
        .select_related("flock", "saloon")
        .prefetch_related("extras")
        .order_by("-report_date", "-id")
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def compat_report_list(request, farmSlug, salonSlug):
    """W1 — the Flutter calendar/history list. Delegates to the canonical
    saloon_reports (from/to/status semantics preserved verbatim); only
    ?status=incomplete is answered here (DRAFT+SUBMITTED — the Flutter
    dashboard queue; the canonical list filters one exact status)."""
    farm, saloon = svc.compat_resolve_farm_saloon(farmSlug, salonSlug, _org_claim(request))
    if farm is None or saloon is None:
        return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    window, error = _report_window_or_400(request)
    if error:
        return error
    if request.query_params.get("status") == "incomplete":
        date_from, date_to = window
        qs = _merged_report_qs(
            saloon_id=saloon.pk, org_id=saloon.org_id, status__in=["DRAFT", "SUBMITTED"]
        )
        if date_from:
            qs = qs.filter(report_date__gte=date_from)
        if date_to:
            qs = qs.filter(report_date__lte=date_to)
        return Response([reports_api._report_list_item(r) for r in qs])
    # Delegation passes the RAW HttpRequest: the canonical views are
    # @api_view-wrapped, so handing them the already-wrapped DRF Request
    # would re-enter initialize_request and fail its HttpRequest assert.
    # request._request carries the same headers/body/middleware claims
    # stash, so auth, roles and reporter=JWT-user re-resolve identically.
    return reports_api.saloon_reports(request._request, saloon.pk)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def compat_report_by_date(request, farmSlug, salonSlug, report_date):
    """W2 — calendar day selection. Delegates to saloon_report_by_date:
    absent → 404, bad date → 400, multi-flock day → 400 multiple-reports."""
    farm, saloon = svc.compat_resolve_farm_saloon(farmSlug, salonSlug, _org_claim(request))
    if farm is None or saloon is None:
        return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    return reports_api.saloon_report_by_date(request._request, saloon.pk, report_date)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def compat_farm_report_list(request, farmSlug):
    """W5 — farm-level dashboard queue. Same window validation and merged
    bare-list shape as W1, scoped to every saloon of the farm; native v1.5
    callers keep the enum values, ?status=incomplete is DRAFT+SUBMITTED."""
    _, farm = svc.compat_resolve_farm(farmSlug, _org_claim(request))
    if farm is None:
        return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    window, error = _report_window_or_400(request)
    if error:
        return error
    date_from, date_to = window
    qs = _merged_report_qs(farm_id=farm.pk, org_id=farm.org_id)
    if date_from:
        qs = qs.filter(report_date__gte=date_from)
    if date_to:
        qs = qs.filter(report_date__lte=date_to)
    wanted = request.query_params.get("status")
    if wanted == "incomplete":
        qs = qs.filter(status__in=["DRAFT", "SUBMITTED"])
    elif wanted:
        qs = qs.filter(status=wanted)
    return Response([reports_api._report_list_item(r) for r in qs])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def compat_report_create(request):
    """W3 — the reports-only compat-write exception. Body carries farm_slug
    + salon_slug (both required; must agree — the salon's farm == the farm,
    mismatch is 404, never a 400 oracle), then delegates to the canonical
    POST: aliases, checklist, FLAGGED auto-ticket and reporter=JWT-user
    ride along untouched (saloon_id travels as the URL arg — the canonical
    view never reads it, or the slugs, from the body)."""
    data = request.data
    if not isinstance(data, dict):
        return Response(
            {"error": "farm_slug and salon_slug are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    farm_slug, salon_slug = data.get("farm_slug"), data.get("salon_slug")
    if not farm_slug or not salon_slug:
        return Response(
            {"error": "farm_slug and salon_slug are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    farm, saloon = svc.compat_resolve_farm_saloon(farm_slug, salon_slug, _org_claim(request))
    if farm is None or saloon is None:
        return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    return reports_api.saloon_reports(request._request, saloon.pk)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def compat_report_edit(request, report_id):
    """W4 — delegates to report_edit: aliases accepted, same-day lock,
    in-play checklist scoping and staff-exempt audit ride along."""
    return reports_api.report_edit(request._request, report_id)
