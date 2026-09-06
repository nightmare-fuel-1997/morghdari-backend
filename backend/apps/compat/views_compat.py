"""Compat REST views — C1..C15 of the route table (M6-09, spec §3/§8).

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
