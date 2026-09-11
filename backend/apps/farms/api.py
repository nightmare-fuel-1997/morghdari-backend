"""farms REST API — M6-01 v1.1 operability set (contract v1.5.0).

- GET  /saloons/{saloon_id}/            → SaloonDetail (sensor slots + position labels)
- GET  /farms/{farm_id}/overview/       → FarmOverview (saloon cards w/ alert counts)
- POST /saloons/{saloon_id}/operational-state/ → canonical loop transition
- GET/POST /saloons/{saloon_id}/flocks/ → flock cycles (register opens a cycle)

Org scoping is explicit in every queryset (org_id from the JWT claims) — RLS is
the production backstop, never the only gate. Writes require engineer+ roles
(farms/engineer work surface); reads are org-member. A never-transitioned
saloon reads EMPTY_SANITATION in the API (the alerting hash keeps its own
ACTIVE_FLOCK default so flock rules stay armed).

فارسی: سطح REST مزرعه/سالن نسخهٔ 0.6 — جزئیات سالن، نمای مزرعه، چرخهٔ وضعیت و
دوره‌های پرورش؛ همیشه با فیلتر org از ادعاهای توکن.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.alerts.models import AlertInstance, SaloonOperationalState
from apps.common.schemas import TAG_FARMS, error_responses
from apps.devices.models import GatewaySaloonZone, SaloonSensorSlot
from apps.farms.serializers import (
    FarmOverviewSerializer,
    FlockCycleCreateResponseSerializer,
    FlockCycleCreateSerializer,
    FlockCycleSerializer,
    OperationalStateRequestSerializer,
    OperationalStateResponseSerializer,
    SaloonDetailSerializer,
)
from apps.farms.models import Farm, Saloon
from apps.system_config.models import SystemConfiguration
from apps.telemetry.models import FlockCycle

TEHRAN = ZoneInfo("Asia/Tehran")

# Engineer-and-above for write surfaces; any org member reads.
WRITE_ROLES = {"engineer", "manager", "owner"}
# The canonical loop (Blueprint §11.3): sanitise → pre-heat → active → harvest → …
TRANSITIONS = {
    "EMPTY_SANITATION": {"PRE_HEATING"},
    "PRE_HEATING": {"ACTIVE_FLOCK"},
    "ACTIVE_FLOCK": {"HARVESTED_VACANT"},
    "HARVESTED_VACANT": {"EMPTY_SANITATION"},
}
PREHEAT_TARGET_KEY = "preheat_target_c"
ACTIVE_FLOCK_TOLERANCE_KEY = "active_flock_temp_tolerance_c"


def _org_id(request):
    return _claims(request).get("org_id")


def _roles(request):
    return set(_claims(request).get("roles", []))


def _saloon_in_org(saloon_id: int, org_id) -> Saloon | None:
    return (
        Saloon.objects.select_related("farm", "farm__province", "farm__city", "farm__village")
        .filter(org_id=org_id, pk=saloon_id)
        .first()
    )


def _config_float(key: str, default: float) -> float:
    raw = SystemConfiguration.objects.filter(key=key).values_list("value", flat=True).first()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _flock_age_days(start_date: datetime, now: datetime) -> int:
    """Whole flock-age days in Tehran-local time (same rule as alerts thresholds)."""
    start_local = start_date.astimezone(TEHRAN).date()
    now_local = now.astimezone(TEHRAN).date()
    return (now_local - start_local).days


def _open_flock(saloon_id: int) -> FlockCycle | None:
    return (
        FlockCycle.objects.filter(saloon_id=saloon_id, end_date__isnull=True)
        .order_by("-start_date")
        .first()
    )


def _operational_state(saloon_id: int) -> str:
    latest = (
        SaloonOperationalState.objects.filter(saloon_id=saloon_id, exited_at__isnull=True)
        .order_by("-entered_at", "-id")
        .values_list("state", flat=True)
        .first()
    )
    # A saloon no one has transitioned yet is an untouched building: EMPTY.
    # NOTE: the ALERTING hash (alerts.services.thresholds) intentionally uses
    # its own "ACTIVE_FLOCK" fallback — a flock that never went through the
    # endpoint must keep its rules armed. The API reports the physical truth.
    # فارسی: سالنی که هیچ‌وقت جابه‌جا نشده یعنی هنوز خالی است؛ موتورِ هشدار اما
    # پیش‌فرض خودش را دارد تا قواعدِ دورهٔ فعال بی‌صدا نشوند.
    return latest or "EMPTY_SANITATION"


def _gateway_binding(saloon_id: int) -> GatewaySaloonZone | None:
    """The gateway slot currently serving a saloon, if any."""
    return (
        GatewaySaloonZone.objects.filter(saloon_id=saloon_id)
        .select_related("gateway_slot")
        .order_by("-assigned_at")
        .first()
    )


def _gateway_statuses(zone_rows) -> dict:
    """Best-effort ONLINE/OFFLINE from the gateway:status:{slot} key.

    Redis unavailability degrades to all-null (contract allows null gateway
    status) instead of failing a read endpoint — liveness is a liveness
    concern, not a data concern.
    فارسی: وضعیتِ زندهٔ گیت‌وی از ردیس خوانده می‌شود؛ نبودِ ردیس یعنی null، نه ۵۰۰.
    """
    slot_ids = [z.gateway_slot_id for z in zone_rows]
    if not slot_ids:
        return {}
    try:
        import os

        import redis as redis_lib

        url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
        client = redis_lib.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        values = client.mget([f"gateway:status:{s}" for s in slot_ids])
        return {
            slot: (values[i].decode() if isinstance(values[i], bytes) else values[i])
            for i, slot in enumerate(slot_ids)
            if values[i] is not None
        }
    except Exception:  # noqa: BLE001 — any redis failure degrades, never 500s
        return {}


def _locations(farm) -> dict:
    province = farm.province
    city = farm.city
    village = farm.village
    return {
        "province_id": province.id,
        "province_name": province.name,
        "city_id": city.id,
        "city_name": city.name,
        "village_id": village.id if village else None,
        "village_name": village.name if village else None,
    }


def _sensor_slots(saloon_id: int) -> list:
    rows = (
        SaloonSensorSlot.objects.filter(saloon_id=saloon_id)
        .select_related("position")
        .order_by("slot_id")
    )
    return [
        {
            "slot_id": row.slot_id,
            "position_id": row.position_id,
            "position_label": row.position.label if row.position_id else None,
            "is_active": bool(row.is_active),
        }
        for row in rows
    ]


def _gateway_out(saloon, zone_rows, statuses: dict) -> tuple:
    """(gateway_slot_id, gateway_status) for one saloon."""
    zone = next((z for z in zone_rows if z.saloon_id == saloon.pk), None)
    if zone is None:
        return None, None
    return zone.gateway_slot_id, statuses.get(zone.gateway_slot_id)


# ---------------------------------------------------------------------------
# GET /saloons/{saloon_id}/
# ---------------------------------------------------------------------------
@extend_schema(
    tags=[TAG_FARMS],
    operation_id="get_saloon_detail",
    summary="Saloon detail",
    description="""The full saloon record: identity, geography, the canonical
`operational_state` loop position, flock age, gateway binding and the sensor
slot list.

Nulls are honest: `gateway_slot_id`/`gateway_status` are null for a saloon
with no zone assignment (or when Redis cannot answer the status key), and
`flock_age_days` is null without an open flock. Foreign/unknown saloon → 404.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        )
    ],
    request=None,
    responses={200: SaloonDetailSerializer, **error_responses(401, 404)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_detail(request, saloon_id: int):
    org_id = _org_id(request)
    saloon = _saloon_in_org(saloon_id, org_id) if org_id else None
    if saloon is None:
        return Response({"error": "saloon not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(_saloon_detail_payload(saloon))


def _saloon_detail_payload(saloon: Saloon) -> dict:
    """The SaloonDetail body — shared by GET /saloons/{id}/ and the M7-11 list."""
    flock = _open_flock(saloon.pk)
    zone_rows = list(GatewaySaloonZone.objects.filter(saloon_id=saloon.pk))
    statuses = _gateway_statuses(zone_rows)
    gateway_slot_id, gateway_status = _gateway_out(saloon, zone_rows, statuses)

    return {
        "saloon_id": str(saloon.pk),
        "name": saloon.name,
        "farm_id": saloon.farm_id,
        "farm_name": saloon.farm.name,
        "locations": _locations(saloon.farm),
        "org_id": str(saloon.org_id),
        "operational_state": _operational_state(saloon.pk),
        "flock_age_days": (
            _flock_age_days(flock.start_date, timezone.now()) if flock else None
        ),
        "gateway_slot_id": gateway_slot_id,
        "gateway_status": gateway_status,
        "sensor_slots": _sensor_slots(saloon.pk),
    }


# ---------------------------------------------------------------------------
# GET /saloons/  (M7-11 — v1.6 native lists)
# ---------------------------------------------------------------------------
@extend_schema(
    tags=[TAG_FARMS],
    operation_id="list_saloons",
    summary="List saloons (v1.6 native list)",
    description="""Bare list (no envelope) of SaloonDetail objects for the caller's org,
ordered by farm then saloon.

`?farm_id=` narrows to one farm. A foreign **and** an unknown farm id share one
verdict — `403 {"error": "forbidden"}` — so the filter can never be used as an
existence oracle for another org's farms.""",
    parameters=[
        OpenApiParameter(
            "farm_id",
            int,
            OpenApiParameter.QUERY,
            required=False,
            description="Narrow to one farm; a foreign or unknown id is a 403.",
        )
    ],
    request=None,
    responses={200: SaloonDetailSerializer(many=True), **error_responses(400, 401, 403)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_list(request):
    """GET /saloons/ — SaloonDetail[] for the caller's org (v1.6 native lists).

    ?farm_id= narrows to one farm; a foreign OR unknown farm gets the SAME
    403 (no existence oracle). Cross-org exposure stays impossible: the
    queryset always carries the claim org — RLS is the Postgres backstop,
    never the only gate.
    فارسی: فهرست سالن‌های سازمان از ادعای توکن؛ فیلترِ مزرعهٔ بیگانه ۴۰۳ است.
    """
    org_id = _org_id(request)
    if org_id is None:
        return Response({"error": "organization context required"}, status=400)

    queryset = (
        Saloon.objects.select_related(
            "farm", "farm__province", "farm__city", "farm__village"
        )
        .filter(org_id=org_id)
        .order_by("farm_id", "id")
    )

    raw_farm_id = str(request.query_params.get("farm_id", "")).strip()
    if raw_farm_id:
        try:
            farm_id = int(raw_farm_id)
        except ValueError:
            return Response({"error": "farm_id must be an integer"}, status=400)
        # Foreign AND unknown share one verdict — the caller cannot tell them apart.
        if not Farm.objects.filter(org_id=org_id, pk=farm_id).exists():
            return Response({"error": "forbidden"}, status=403)
        queryset = queryset.filter(farm_id=farm_id)

    return Response([_saloon_detail_payload(row) for row in queryset])


# ---------------------------------------------------------------------------
# GET /farms/{farm_id}/overview/
# ---------------------------------------------------------------------------
@extend_schema(
    tags=[TAG_FARMS],
    operation_id="get_farm_overview",
    summary="Farm overview",
    description="""The farm header plus one rollup card per saloon: state, flock age,
gateway status and the active-alert aggregate (`active_alert_count`,
`highest_active_tier`).

Aggregates are computed in bulk (no N+1) and the alert tier is null when the
saloon has no unresolved alert. Foreign/unknown farm → 404.""",
    parameters=[
        OpenApiParameter(
            "farm_id", int, OpenApiParameter.PATH, description="Farm id (caller's org)."
        )
    ],
    request=None,
    responses={200: FarmOverviewSerializer, **error_responses(401, 404)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farm_overview(request, farm_id: int):
    org_id = _org_id(request)
    farm = (
        Farm.objects.select_related("province", "city", "village")
        .filter(org_id=org_id, pk=farm_id)
        .first()
    ) if org_id else None
    if farm is None:
        return Response({"error": "farm not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(_farm_overview_payload(farm))


def _farm_overview_payload(farm: Farm) -> dict:
    """The FarmOverview body — shared by GET /farms/{id}/overview/ and the M7-11 list."""
    saloons = list(Saloon.objects.filter(org_id=farm.org_id, farm_id=farm.pk).order_by("id"))
    saloon_ids = [s.pk for s in saloons]

    # Open flock per saloon + active-alert aggregates in bulk (never N+1).
    flock_by_saloon = {
        flock.saloon_id: flock
        for flock in FlockCycle.objects.filter(saloon_id__in=saloon_ids, end_date__isnull=True)
        .order_by("saloon_id", "-start_date")
    }
    zone_rows = list(GatewaySaloonZone.objects.filter(saloon_id__in=saloon_ids))
    statuses = _gateway_statuses(zone_rows)
    active = AlertInstance.objects.filter(saloon_id__in=saloon_ids, resolved_at__isnull=True)
    alert_stats = {
        row["saloon_id"]: row
        for row in active.values("saloon_id").annotate(
            count=Count("id"), top=Max("highest_tier_reached")
        )
    }

    now = timezone.now()
    cards = []
    for saloon in saloons:
        flock = flock_by_saloon.get(saloon.pk)
        stats = alert_stats.get(saloon.pk) or {"count": 0, "top": None}
        gateway_slot_id, gateway_status = _gateway_out(saloon, zone_rows, statuses)
        cards.append({
            "saloon_id": str(saloon.pk),
            "name": saloon.name,
            "operational_state": _operational_state(saloon.pk),
            "flock_age_days": _flock_age_days(flock.start_date, now) if flock else None,
            "gateway_status": gateway_status,
            "gateway_slot_id": gateway_slot_id,
            "active_alert_count": stats["count"],
            "highest_active_tier": stats["top"],
        })

    return {
        "farm_id": farm.pk,
        "name": farm.name,
        "locations": _locations(farm),
        "saloons": cards,
    }


# ---------------------------------------------------------------------------
# GET /farms/  (M7-11 — v1.6 native lists)
# ---------------------------------------------------------------------------
@extend_schema(
    tags=[TAG_FARMS],
    operation_id="list_farms",
    summary="List farms (v1.6 native list)",
    description="""Bare list (no envelope) of FarmOverview objects for the caller's org.

`?org_id=` is accepted only when it EQUALS the validated claim org; asking for
another org is a 403 ("you asked for data that is not yours"), never a
misleading empty list.""",
    parameters=[
        OpenApiParameter(
            "org_id",
            str,
            OpenApiParameter.QUERY,
            required=False,
            description="Must equal the caller's claim org when present; otherwise 403.",
        )
    ],
    request=None,
    responses={200: FarmOverviewSerializer(many=True), **error_responses(400, 401, 403)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farm_list(request):
    """GET /farms/ — FarmOverview[] for the caller's org (v1.6 native lists).

    ?org_id= is honored only when it EQUALS the validated claim org — a
    mismatch is a 403 (you asked for another org's data), never an empty
    list. Cross-org exposure stays impossible: every queryset carries the
    claim org; RLS is the Postgres backstop.
    فارسی: فهرست مزرعه‌های سازمان؛ درخواستِ سازمانِ دیگر ۴۰۳ است نه فهرستِ خالی.
    """
    org_id = _org_id(request)
    if org_id is None:
        return Response({"error": "organization context required"}, status=400)

    requested = str(request.query_params.get("org_id", "")).strip()
    if requested and requested != str(org_id):
        return Response({"error": "forbidden"}, status=403)

    farms = (
        Farm.objects.select_related("province", "city", "village")
        .filter(org_id=org_id)
        .order_by("id")
    )
    return Response([_farm_overview_payload(farm) for farm in farms])


# ---------------------------------------------------------------------------
# POST /saloons/{saloon_id}/operational-state/
# ---------------------------------------------------------------------------
@extend_schema(
    tags=[TAG_FARMS],
    operation_id="transition_operational_state",
    summary="Transition the saloon's operational state",
    description="""Moves the saloon one step along the canonical loop
(`EMPTY_SANITATION → PRE_HEATING → ACTIVE_FLOCK → HARVESTED_VACANT → …`) and
closes the previous state row in the same transaction. **Engineer+** only.

Verdicts: 400 for an unknown state, a no-op (already in that state) or an
illegal jump — the loop is strictly one step forward; 403 for a missing role.

The A6 guard: entering `ACTIVE_FLOCK` also requires the latest saloon
temperature to sit within ±`active_flock_temp_tolerance_c` of the cached
`preheat_target_c`, otherwise the transition is a 400 with the measured
distance in the message. With no cached target, no reading, or Redis down the
guard degrades to *allow* — never to a 500.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        )
    ],
    request=OperationalStateRequestSerializer,
    responses={
        200: OperationalStateResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Start pre-heating",
            request_only=True,
            value={"state": "PRE_HEATING", "reason": "New flock arrives tomorrow"},
        )
    ],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def operational_state(request, saloon_id: int):
    """Transition the saloon along the canonical loop; engineer+ only."""
    org_id = _org_id(request)
    if not org_id or not (_roles(request) & WRITE_ROLES):
        return Response({"error": "engineer role required"}, status=403)

    saloon = Saloon.objects.filter(org_id=org_id, pk=saloon_id).first()
    if saloon is None:
        return Response({"error": "saloon not found"}, status=404)

    requested = str(request.data.get("state", "")).strip().upper()
    if requested not in TRANSITIONS:
        return Response(
            {"error": "state must be one of " + ", ".join(TRANSITIONS)},
            status=400,
        )

    current = _operational_state(saloon.pk)
    if requested == current:
        return Response(
            {"error": f"saloon is already in state {current}"}, status=400
        )
    if requested not in TRANSITIONS[current]:
        return Response(
            {"error": f"illegal transition {current} → {requested} (canonical loop)"},
            status=400,
        )

    # A6 guard: ACTIVE_FLOCK may not open while temp sits outside
    # ±active_flock_temp_tolerance_c of the pre-heat target (Redis hash field
    # `preheat_target_c`). Enforced only when the thresholds hash is reachable —
    # without it there is nothing to compare against (doc: blocked/warned per
    # config; no config → no block).
    if requested == "ACTIVE_FLOCK":
        tolerance = _config_float(ACTIVE_FLOCK_TOLERANCE_KEY, 2.0)
        warning = _warn_on_temp_violation(saloon.pk, tolerance)
        if warning:
            return Response({"error": warning}, status=400)

    # HTTP tenant scoping is the middleware's GUC (set once per request) —
    # the same convention as the provisioning views; no nested tenant_context.
    with transaction.atomic():
        SaloonOperationalState.objects.filter(
            saloon=saloon, exited_at__isnull=True
        ).update(exited_at=timezone.now())
        SaloonOperationalState.objects.create(
            saloon=saloon,
            org_id=org_id,
            state=requested,
            triggered_by=request.user,
            reason=str(request.data.get("reason", "")).strip() or None,
        )
    return Response(
        {"saloon_id": str(saloon.pk), "operational_state": requested},
        status=status.HTTP_200_OK,
    )


def _warn_on_temp_violation(saloon_id: int, tolerance: float):
    """Return an error string when the ACTIVE_FLOCK temp guard trips, else None.

    Compare the latest known temp avg (from telemetry) against the cached
    pre-heat target in the thresholds hash. Redis unavailability, a missing
    target, or no reading yet ⇒ no guard (nothing to compare).
    فارسی: نگهبانِ دما — اگر میانگینِ آخرین دمای سالن از هدفِ پیش‌گرمایش بیشتر از
    تلورانس فاصله داشت، ورود به ACTIVE_FLOCK بسته می‌شود.
    """
    from apps.alerts.services.redis_keys import thresholds_hash
    from apps.telemetry.models import SaloonTelemetry

    try:
        import os

        import redis as redis_lib

        url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
        client = redis_lib.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        raw_target = client.hget(thresholds_hash(saloon_id), PREHEAT_TARGET_KEY)
    except Exception:  # noqa: BLE001 — guard degrades to allow, never to 500
        return None
    if raw_target is None:
        return None
    try:
        target = float(raw_target)
    except (TypeError, ValueError):
        return None
    latest = (
        SaloonTelemetry.objects.filter(saloon_id=saloon_id, is_buffered=False)
        .order_by("-device_ts")
        .first()
    )
    if latest is None:
        return None
    temp = (latest.metrics or {}).get("temp")
    current = temp.get("avg") if isinstance(temp, dict) else None
    if current is None:
        return None
    try:
        current = float(current)
    except (TypeError, ValueError):
        return None
    if abs(current - target) > tolerance:
        return (
            f"current temp {current}°C is outside ±{tolerance}°C of the "
            f"pre-heat target {target}°C (active_flock_temp_tolerance_c)"
        )
    return None


# ---------------------------------------------------------------------------
# GET/POST /saloons/{saloon_id}/flocks/
# ---------------------------------------------------------------------------
@extend_schema(
    methods=["GET"],
    tags=[TAG_FARMS],
    operation_id="list_flock_cycles",
    summary="List flock cycles",
    description="""Bare list of the saloon's flock cycles, newest start first. Dates are the
**Tehran calendar days** the farm meant (`YYYY-MM-DD`), not UTC instants;
`end_date` is null while the flock is open. Foreign/unknown saloon → 404.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        )
    ],
    request=None,
    responses={200: FlockCycleSerializer(many=True), **error_responses(400, 401, 404)},
)
@extend_schema(
    methods=["POST"],
    tags=[TAG_FARMS],
    operation_id="create_flock_cycle",
    summary="Register a flock cycle",
    description="""Opens a new flock for the saloon (**engineer+**). `breed_type`,
`start_date` and `placement_count` (≥1 — it is the mortality-% denominator)
are required; `day_old_weight_g` is optional FCR input. The new row keeps
`end_date` null, i.e. it becomes the active flock.

Creating a second flock while one is open is a 400 — a saloon has at most one
active flock. Registering a flock re-arms the saloon's threshold bands
immediately (post_save cache hook).""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        )
    ],
    request=FlockCycleCreateSerializer,
    responses={
        201: FlockCycleCreateResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Register a flock",
            request_only=True,
            value={
                "breed_type": "ross308",
                "start_date": "2026-09-01",
                "placement_count": 5000,
                "day_old_weight_g": 42.0,
            },
        )
    ],
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def flock_cycles(request, saloon_id: int):
    """List flock cycles (GET, org member) or register one (POST, engineer+).

    POST contract v1.4.2: placement_count REQUIRED (≥ 1) — it is the
    mortality-% denominator, NOT NULL in database.md. The post_save signal
    (alerts) triggers cache_saloon_thresholds immediately; creating over an
    active flock → 400. Two requests, one view — same route, both methods.
    """
    if request.method == "GET":
        return _flock_list(request, saloon_id)
    return _flock_create(request, saloon_id)


def _flock_list(request, saloon_id: int):
    org_id = _org_id(request)
    if org_id is None:
        return Response({"error": "organization context required"}, status=400)
    if Saloon.objects.filter(org_id=org_id, pk=saloon_id).exists():
        rows = FlockCycle.objects.filter(saloon_id=saloon_id).order_by("-start_date")
        return Response([
            {
                "flock_id": row.flock_id,
                "breed_type": row.breed_type,
                "start_date": _flock_date(row.start_date),
                "end_date": _flock_date(row.end_date),
            }
            for row in rows
        ])
    return Response({"error": "saloon not found"}, status=404)


def _flock_create(request, saloon_id: int):
    """Register a flock cycle (engineer+); end_date stays NULL = active flock."""
    org_id = _org_id(request)
    if not org_id or not (_roles(request) & WRITE_ROLES):
        return Response({"error": "engineer role required"}, status=403)
    saloon = Saloon.objects.filter(org_id=org_id, pk=saloon_id).first()
    if saloon is None:
        return Response({"error": "saloon not found"}, status=404)

    if _open_flock(saloon.pk) is not None:
        return Response(
            {"error": "an active flock already exists for this saloon"}, status=400
        )

    breed_type = str(request.data.get("breed_type", "")).strip()
    if not breed_type:
        return Response({"error": "breed_type required"}, status=400)

    from django.utils.dateparse import parse_date, parse_datetime

    raw_start = str(request.data.get("start_date", "")).strip()
    parsed = parse_datetime(raw_start)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TEHRAN)
    parsed = parsed or _date_to_datetime(parse_date(raw_start))
    if parsed is None:
        return Response({"error": "start_date required (YYYY-MM-DD or ISO datetime)"}, status=400)

    raw_count = request.data.get("placement_count")
    try:
        placement_count = int(raw_count)
    except (TypeError, ValueError):
        return Response({"error": "placement_count required and must be an integer"}, status=400)
    if placement_count < 1:
        return Response({"error": "placement_count must be ≥ 1"}, status=400)

    day_old_weight = request.data.get("day_old_weight_g")
    try:
        day_old_weight_g = float(day_old_weight) if day_old_weight not in (None, "") else None
    except (TypeError, ValueError):
        return Response({"error": "day_old_weight_g must be numeric"}, status=400)

    with transaction.atomic():
        flock = FlockCycle.objects.create(
            org_id=org_id,
            saloon=saloon,
            breed_type=breed_type,
            start_date=parsed,
            placement_count=placement_count,
            day_old_weight_g=day_old_weight_g,
        )
    return Response(
        {
            "flock_id": flock.flock_id,
            "breed_type": flock.breed_type,
            "start_date": _flock_date(flock.start_date),
            "end_date": None,
            "placement_count": flock.placement_count,
        },
        status=status.HTTP_201_CREATED,
    )


def _flock_date(value) -> str | None:
    """Contract date = the Tehran calendar day the farm meant (flock dates are
    entered as local days; the stored instant is UTC).
    فارسی: تاریخِ قراردادی همان روزِ تقویمیِ تهران است که مرغ‌دار منظورش بوده.
    """
    if value is None:
        return None
    return value.astimezone(TEHRAN).date().isoformat()


def _date_to_datetime(parsed_date):
    if parsed_date is None:
        return None
    return datetime.combine(parsed_date, datetime.min.time()).replace(tzinfo=TEHRAN)
