"""Compat REST views — C1..C15 of the route table (M6-09, spec §3/§8) +
W1..W5 report aliases (M7-17, docs/api-reports-merge-v1.md §5: thin shims
over the canonical merge surface — slugs via legacy_slug_map, reporter is
always the JWT user, never client input).

Thin by design: auth (DRF IsAuthenticated; the tenant middleware already
verified the Bearer token and armed the GUC) → compat_resolve_* (charset +
org-claim check; a miss is ALWAYS 404 with the spec's error strings, never
500, never an empty 200) → serialize with exact Flutter keys. Slugs are also
route-validated (<slug:...> already constrains to [-a-zA-Z0-9_]; the
services' regex is the stricter [A-Za-z0-9-]{1,64} authority).

Global error contract: {"error": "<string>"} with the spec §8 codes —
404 unknown slug / no telemetry · 400 inverted window / bad charset.

فارسی: ویوهای نازلِ سازگاری — هر خطای اسلاگِ خارجی یا بد، ۴۰۴ تمیز است.
"""

from datetime import datetime as _datetime

from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.compat import serializers_compat as ser
from apps.compat import services_compat as svc
from apps.compat.models import LegacySlugMap
from apps.devices.models import GatewaySaloonZone
from apps.farms.models import Farm, Saloon
from apps.reports import api as reports_api
from apps.reports.models import DailyReport
from apps.telemetry.models import SaloonTelemetry


def _org_claim(request):
    return _claims(request).get("org_id")


def _org_slug_row(org_id) -> LegacySlugMap | None:
    return LegacySlugMap.objects.filter(org_id=org_id, kind="org").order_by("slug").first()


def _farm_slug_rows(org_id) -> dict:
    return {
        row.farm_id: row.slug
        for row in LegacySlugMap.objects.filter(org_id=org_id, kind="farm")
    }


def _device_slug_rows(org_id) -> dict:
    return {
        row.saloon_id: row.slug
        for row in LegacySlugMap.objects.filter(org_id=org_id, kind="device")
    }


# ---------------------------------------------------------------------------
# Orgs (C1..C4)
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def orgs_list(request):
    claim = _org_claim(request)
    if not claim:
        return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    row = _org_slug_row(claim)
    if row is None:
        return Response({"error": "org not found"}, status=status.HTTP_404_NOT_FOUND)
    _, org = svc.compat_resolve_org(row.slug, claim)
    if org is None:
        return Response({"error": "org not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response([ser.org_item(row, org)])  # raw array — Flutter reads a list


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def org_detail(request, orgSlug):
    _, org = svc.compat_resolve_org(orgSlug, _org_claim(request))
    if org is None:
        return Response({"error": "org not found"}, status=status.HTTP_404_NOT_FOUND)
    row = _org_slug_row(org.org_id)
    return Response(ser.org_item(row, org))


def _org_farms(org_id):
    return list(Farm.objects.filter(org_id=org_id).order_by("pk"))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def org_farms(request, orgSlug):
    row, org = svc.compat_resolve_org(orgSlug, _org_claim(request))
    if org is None:
        return Response({"error": "org not found"}, status=status.HTTP_404_NOT_FOUND)
    farm_slugs = _farm_slug_rows(org.org_id)
    return Response([
        ser.farm_item(LegacySlugMap(slug=slug), farm, row)
        for farm in _org_farms(org.org_id)
        for slug in [farm_slugs.get(farm.pk, "")]
        if slug
    ])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def org_devices(request, orgSlug):
    row, org = svc.compat_resolve_org(orgSlug, _org_claim(request))
    if org is None:
        return Response({"error": "org not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(_device_items(org.org_id, Saloon.objects.filter(org_id=org.org_id)))


# ---------------------------------------------------------------------------
# Farms (C5..C7)
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farms_list(request):
    claim = _org_claim(request)
    if not claim:
        return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    org_row = _org_slug_row(claim)
    if org_row is None:
        return Response({"error": "farm not found"}, status=status.HTTP_404_NOT_FOUND)
    farm_slugs = _farm_slug_rows(claim)
    return Response([
        ser.farm_item(LegacySlugMap(slug=slug), farm, org_row)
        for farm in _org_farms(claim)
        for slug in [farm_slugs.get(farm.pk, "")]
        if slug
    ])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farm_detail(request, farmSlug):
    row, farm = svc.compat_resolve_farm(farmSlug, _org_claim(request))
    if farm is None:
        return Response({"error": "farm not found"}, status=status.HTTP_404_NOT_FOUND)
    org_row = _org_slug_row(farm.org_id)
    return Response(ser.farm_item(row, farm, org_row))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farm_devices(request, farmSlug):
    row, farm = svc.compat_resolve_farm(farmSlug, _org_claim(request))
    if farm is None:
        return Response({"error": "farm not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(_device_items(farm.org_id, Saloon.objects.filter(org_id=farm.org_id, farm_id=farm.pk)))


# ---------------------------------------------------------------------------
# Devices (C8..C9) — Flutter device == backend saloon
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def devices_list(request):
    claim = _org_claim(request)
    if not claim:
        return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    return Response(_device_items(claim, Saloon.objects.filter(org_id=claim)))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def device_detail(request, deviceSlug):
    row, saloon = svc.compat_resolve_saloon(deviceSlug, _org_claim(request))
    if saloon is None:
        return Response({"error": "device not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(_device_items(saloon.org_id, Saloon.objects.filter(pk=saloon.pk))[0])


# ---------------------------------------------------------------------------
# Device-item assembly (spec §5) — bulk, never N+1
# ---------------------------------------------------------------------------


def _status_from_gateway_status(raw) -> str:
    return str(raw).lower() if raw else ""


def _device_items(org_id, saloons) -> list:
    saloons = list(saloons.order_by("pk"))
    if not saloons:
        return []
    saloon_ids = [s.pk for s in saloons]

    device_slugs = _device_slug_rows(org_id)
    farm_slugs = _farm_slug_rows(org_id)
    org_slug = (_org_slug_row(org_id).slug if _org_slug_row(org_id) else "")

    zone_rows = list(GatewaySaloonZone.objects.filter(saloon_id__in=saloon_ids))
    from apps.farms.api import _gateway_statuses  # additive reuse, no edits

    statuses = _gateway_statuses(zone_rows)
    zone_by_saloon = {z.saloon_id: z for z in zone_rows}

    if connection.vendor == "postgresql":
        latest = {
            row["saloon_id"]: row
            for row in (
                SaloonTelemetry.objects.filter(saloon_id__in=saloon_ids)
                .order_by("saloon_id", "-server_received_ts")
                .distinct("saloon_id")
                .values("saloon_id", "server_received_ts", "metrics")
            )
        }
    else:
        # Portable fallback (sqlite quick loop): ascending order means the last
        # row per saloon wins — identical semantics to the DISTINCT ON branch.
        latest = {}
        for row in (
            SaloonTelemetry.objects.filter(saloon_id__in=saloon_ids)
            .order_by("saloon_id", "server_received_ts")
            .values("saloon_id", "server_received_ts", "metrics")
        ):
            latest[row["saloon_id"]] = row

    items = []
    for saloon in saloons:
        slug = device_slugs.get(saloon.pk)
        if not slug:
            continue  # a saloon without its slug row is simply not a device yet
        zone = zone_by_saloon.get(saloon.pk)
        gateway_status = statuses.get(zone.gateway_slot_id) if zone else None
        last = latest.get(saloon.pk)

        allowed = set()
        from apps.devices.models import SaloonSensorSlot

        for slot in SaloonSensorSlot.objects.filter(
            saloon_id=saloon.pk, org_id=org_id, is_active=True
        ):
            allowed.update(slot.allowed_metric_keys or [])
        if last and isinstance(last["metrics"], dict):
            allowed.update(k for k in last["metrics"] if isinstance(k, str))

        items.append(ser.device_item(
            LegacySlugMap(slug=slug),
            saloon,
            farm_slug=farm_slugs.get(saloon.farm_id, ""),
            org_slug=org_slug,
            status=_status_from_gateway_status(gateway_status),
            last_seen_at=svc.iso_z(last["server_received_ts"]) if last else "",
            capabilities=svc.flutter_metric_names(allowed),
            created_at=svc.iso_z(saloon.created_at),
        ))
    return items


# ---------------------------------------------------------------------------
# Telemetry (C10..C15) — flat records over the hypertable
# ---------------------------------------------------------------------------

HISTORY_DEFAULT_LIMIT = 100
FARM_HISTORY_DEFAULT_LIMIT = 24  # the shipped chart call sends limit: 24
HISTORY_MAX_LIMIT = 1000


def _history_response(rows, saloon_slug_by_id) -> Response:
    return Response([
        svc.compat_flatten_reading(row, saloon_slug_by_id.get(row.saloon_id, ""))
        for row in rows
    ])


def _window_or_400(request, default_limit):
    window = svc.resolve_history_window(request.query_params, default_limit=default_limit)
    if window is None:
        return None, Response(
            {"error": "invalid time window"}, status=status.HTTP_400_BAD_REQUEST
        )
    return window, None


def _history_queryset(org_id, saloon_ids, since, until, limit, offset):
    """Newest-first page of committed rows; clock_drift_ms is derived from the
    ts delta inside compat_flatten_reading (same definition as the
    DB-generated buffered_delay_mins column)."""
    queryset = SaloonTelemetry.objects.filter(org_id=org_id, saloon_id__in=saloon_ids).order_by(
        "-server_received_ts"
    )
    if since is not None:
        queryset = queryset.filter(server_received_ts__gte=since)
    if until is not None:
        queryset = queryset.filter(server_received_ts__lte=until)
    return queryset[offset:offset + limit]


def _latest_response(queryset, slug_by_saloon):
    row = queryset.first()
    if row is None:
        return Response({"error": "no telemetry"}, status=status.HTTP_404_NOT_FOUND)
    return Response(svc.compat_flatten_reading(row, slug_by_saloon.get(row.saloon_id, "")))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def telemetry_org_latest(request, orgSlug):
    _, org = svc.compat_resolve_org(orgSlug, _org_claim(request))
    if org is None:
        return Response({"error": "org not found"}, status=status.HTTP_404_NOT_FOUND)
    slugs = _device_slug_rows(org.org_id)
    if not slugs:
        return Response({"error": "no telemetry"}, status=status.HTTP_404_NOT_FOUND)
    queryset = SaloonTelemetry.objects.filter(org_id=org.org_id, saloon_id__in=list(slugs)).order_by(
        "-server_received_ts"
    )
    return _latest_response(queryset, slugs)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def telemetry_org_history(request, orgSlug):
    _, org = svc.compat_resolve_org(orgSlug, _org_claim(request))
    if org is None:
        return Response({"error": "org not found"}, status=status.HTTP_404_NOT_FOUND)
    window, error = _window_or_400(request, HISTORY_DEFAULT_LIMIT)
    if error:
        return error
    since, until, limit, offset = window
    slugs = _device_slug_rows(org.org_id)
    rows = _history_queryset(org.org_id, list(slugs), since, until, limit, offset)
    return _history_response(rows, slugs)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def telemetry_farm_latest(request, farmSlug):
    _, farm = svc.compat_resolve_farm(farmSlug, _org_claim(request))
    if farm is None:
        return Response({"error": "farm not found"}, status=status.HTTP_404_NOT_FOUND)
    slugs = _device_slug_rows(farm.org_id)
    queryset = SaloonTelemetry.objects.filter(org_id=farm.org_id, saloon__farm_id=farm.pk).order_by(
        "-server_received_ts"
    )
    return _latest_response(queryset, slugs)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def telemetry_farm_history(request, farmSlug):
    _, farm = svc.compat_resolve_farm(farmSlug, _org_claim(request))
    if farm is None:
        return Response({"error": "farm not found"}, status=status.HTTP_404_NOT_FOUND)
    window, error = _window_or_400(request, FARM_HISTORY_DEFAULT_LIMIT)
    if error:
        return error
    since, until, limit, offset = window
    slugs = _device_slug_rows(farm.org_id)
    rows = _history_queryset(farm.org_id, _farm_saloon_ids(farm), since, until, limit, offset)
    body = _history_response(rows, slugs).data
    return Response({"farm_history": body})  # the EXACT key the chart reads


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def telemetry_device_latest(request, deviceSlug):
    row, saloon = svc.compat_resolve_saloon(deviceSlug, _org_claim(request))
    if saloon is None:
        return Response({"error": "device not found"}, status=status.HTTP_404_NOT_FOUND)
    queryset = SaloonTelemetry.objects.filter(org_id=saloon.org_id, saloon_id=saloon.pk).order_by(
        "-server_received_ts"
    )
    return _latest_response(queryset, {saloon.pk: row.slug})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def telemetry_device_history(request, deviceSlug):
    row, saloon = svc.compat_resolve_saloon(deviceSlug, _org_claim(request))
    if saloon is None:
        return Response({"error": "device not found"}, status=status.HTTP_404_NOT_FOUND)
    window, error = _window_or_400(request, HISTORY_DEFAULT_LIMIT)
    if error:
        return error
    since, until, limit, offset = window
    rows = _history_queryset(
        saloon.org_id, [saloon.pk], since, until, limit, offset
    )
    return _history_response(rows, {saloon.pk: row.slug})


def _farm_saloon_ids(farm) -> list:
    return list(Saloon.objects.filter(org_id=farm.org_id, farm_id=farm.pk).values_list("pk", flat=True))


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
