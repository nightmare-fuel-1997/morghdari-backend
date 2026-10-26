"""farms follow-through API — the topology WRITE routes (contract v1.7.2).

Spec: `docs/api-followthrough-v1.md` F1–F8, F14, F15 (additive-only; zero new
tables, zero migrations — every write touches a frozen table).

WHY a second module: `api.py` is the frozen v1.5/v1.6 read + operability surface.
Two of the new routes are NOT here on purpose, because Django resolves the FIRST
matching pattern regardless of HTTP method — a second pattern for an existing
path would shadow the first and answer 405 (the gotcha the ops wave paid for
with O5/O6). Those two extend the existing views instead:

  POST /farms/                 → extends `farm_list` in api.py (GET+POST)
  PATCH|DELETE /saloons/{id}/  → extends `saloon_detail` in api.py (GET+PATCH+DELETE)

Everything else is a new path and lives here, mounted by `urls_follow.py`.

Roles (spec table): owner for farms, manager+ for saloons/positions, engineer+
for the flock cycle endpoints. Scope rows (`account_role_scopes`, M2-06) NARROW
every write: an unscoped member is org-wide, but a member holding scope rows may
only write inside them — a 403, never a silent widen (the v1.7.1 template wave
established that inversion and this surface keeps it).

Every accepted write lands an `AccountActivityLog` row in the same transaction,
and every foreign/unknown id is an identical 404 (never an existence oracle).

فارسی: مسیرهای نوشتنِ توپولوژی — مزرعه با مالک، سالن/موقعیت با مدیر، دورهٔ پرورش
با مهندس؛ دامنه‌های نقش فقط محدود می‌کنند و هر تغییر ردیف ممیزی می‌نویسد.
"""

from datetime import date, datetime, time as dt_time
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Count, Max
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims, scoped_farms, scoped_saloons
from apps.alerts.models import AlertInstance, MortalityRecord
from apps.auditing.models import AccountActivityLog
from apps.common.schemas import CONFLICT_RESPONSE, TAG_FARMS, error_responses
from apps.compat.legacy_slugs import mint_farm_slug, mint_saloon_slug
from apps.devices.models import SaloonPosition, SaloonSensorSlot
from apps.farms.api import (
    WRITE_ROLES,
    _farm_overview_payload,
    _flock_date,
    _saloon_detail_payload,
    _sensor_slot_out,
)
from apps.farms.models import Farm, Saloon
from apps.farms.serializers import FarmOverviewSerializer, SaloonDetailSerializer
from apps.farms.serializers_follow import (
    FarmCreateRequestSerializer,
    FarmDeleteResponseSerializer,
    FarmUpdateRequestSerializer,
    FlockCloseRequestSerializer,
    FlockCloseResponseSerializer,
    MortalityCreateRequestSerializer,
    MortalityResponseSerializer,
    OverviewResponseSerializer,
    PositionCreateRequestSerializer,
    PositionItemSerializer,
    PositionUpdateRequestSerializer,
    SaloonCreateRequestSerializer,
    SaloonDeleteResponseSerializer,
    SaloonUpdateRequestSerializer,
    SensorSlotResponseSerializer,
    SensorSlotUpdateRequestSerializer,
)
from apps.farms.services_follow import (
    delete_row,
    farm_delete_error,
    resolve_geography,
    saloon_delete_error,
)
from apps.telemetry.models import FlockCycle

TEHRAN = ZoneInfo("Asia/Tehran")
OWNER_ROLES = {"owner"}
MANAGER_ROLES = {"manager", "owner"}
MAX_NAME = 255
MAX_LABEL = 100

# Audit actions — the tenant surface's own vocabulary. The staff topology
# surface (docs/api-topology-write-v1.md §3.6) prefixes the same verbs with
# TOPOLOGY_, so an audit query can tell who wrote a row and through which door.
ACTION_FARM_CREATED = "FARM_CREATED"
ACTION_FARM_UPDATED = "FARM_UPDATED"
ACTION_FARM_DELETED = "FARM_DELETED"
ACTION_SALOON_CREATED = "SALOON_CREATED"
ACTION_SALOON_UPDATED = "SALOON_UPDATED"
ACTION_SALOON_DELETED = "SALOON_DELETED"
ACTION_POSITION_CREATED = "POSITION_CREATED"
ACTION_POSITION_UPDATED = "POSITION_UPDATED"
# 2026-10-26: the sensor slot write surface (retire / re-arm / re-scope). The
# create half lives in api.py and was the only slot write before this.
ACTION_SENSOR_SLOT_UPDATED = "SENSOR_SLOT_UPDATED"
ACTION_SENSOR_SLOT_RETIRED = "SENSOR_SLOT_RETIRED"
ACTION_FLOCK_CLOSED = "FLOCK_CLOSED"
ACTION_MORTALITY_RECORDED = "MORTALITY_RECORDED"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _org_id(request):
    return _claims(request).get("org_id")


def _roles(request) -> set:
    return set(_claims(request).get("roles", []))


def _forbidden(msg: str = "forbidden"):
    return Response({"error": msg}, status=status.HTTP_403_FORBIDDEN)


def _bad_request(msg: str):
    return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)


def _not_found(msg: str = "not found"):
    return Response({"error": msg}, status=status.HTTP_404_NOT_FOUND)


def _conflict(msg: str):
    return Response({"error": msg}, status=status.HTTP_409_CONFLICT)


def _audit(request, action, *, org_id, farm=None, saloon=None, details=None):
    """One immutable audit row per accepted write (same transaction).

    BUG-10/BUG-18: `farm`/`saloon` are passed whenever the row SURVIVES the
    request — the FKs are SET_NULL, so an audit row never blocks the delete
    of the entity it records (it stays behind with a NULL FK and intact
    `details`). Deletions still carry the ids in `details` instead, because
    the entity row is already gone when the *_DELETED line is written.

    فارسی (باگ ۱۰/۱۸): سطر حسابرسی حذفِ موجود را سد نمی‌کند؛ با کلیدِ تهی و
    جزئیاتِ سالم باقی می‌ماند.
    """
    AccountActivityLog.objects.create(
        actor=request.user,
        org_id=org_id,
        action=action,
        farm=farm,
        saloon=saloon,
        ip_address=request.META.get("REMOTE_ADDR"),
        details=details or {},
    )


def _installer_session_farm(request, farm_id) -> bool:
    """AD-14 / task 2.16: may this caller write inside ONE farm as an installer?

    An installer is org-less platform staff; the only reason they can write
    anything at all is the scoped session `install_job_session` minted for a job.
    That token carries `org_id`, `installer_session`, `job_id` and `farm_id` —
    and `farm_id` is the whole authorisation: it names the one farm the installer
    was dispatched to.

    The claim is deliberately NOT `support_session`: this grants no support
    operator's powers, only the ability to describe the farm being installed.
    A missing `farm_id` claim means the session was minted without a farm (which
    is impossible today) — treated as no access rather than all access.
    """
    claims = _claims(request)
    if not claims.get("installer_session"):
        return False
    if not claims.get("org_id") or claims["org_id"] != str(farm_id_org_id(request, farm_id)):
        return False
    return claims.get("farm_id") == farm_id


def farm_id_org_id(request, farm_id):
    """The org of a farm, read through whatever context the caller has.

    Read-only and org-agnostic on purpose: an installer session already proves
    the org, so this is a consistency check, not the authorisation.
    """
    from apps.farms.models import Farm

    return Farm.objects.filter(pk=farm_id).values_list("org_id", flat=True).first()


def _may_write_farm(request, farm_id) -> bool:
    """The single gate both writers use (task 2.16).

    Manager/owner of the org, OR an installer session scoped to THIS farm.
    An installer session for another farm is a 403, not a fallback.
    """
    if _roles(request) & MANAGER_ROLES:
        return True
    return _installer_session_farm(request, farm_id)


def _installer_session_saloon(request, saloon_id) -> bool:
    """Same rule as `_installer_session_farm`, reached through a saloon.

    `position_create` is addressed by saloon, not farm, so the farm the session
    was scoped to has to be resolved before the check can run.
    """
    from apps.farms.models import Saloon

    farm_id = (
        Saloon.objects.filter(pk=saloon_id).values_list("farm_id", flat=True).first()
    )
    return farm_id is not None and _installer_session_farm(request, farm_id)


def _scoped_out_farm(request, farm) -> bool:
    """True when the caller's scope rows exclude this farm (a 403, never a widen)."""
    scoped = scoped_farms(request.user, farm.org_id)
    return scoped is not None and not scoped.filter(pk=farm.pk).exists()


def _scoped_out_saloon(request, saloon) -> bool:
    scoped = scoped_saloons(request.user, saloon.org_id)
    return scoped is not None and not scoped.filter(pk=saloon.pk).exists()


def _clean_name(value) -> str | None:
    name = str(value or "").strip()
    if not name or len(name) > MAX_NAME:
        return None
    return name


def _parse_day(raw):
    """`YYYY-MM-DD` (or an ISO datetime's day part) → date, else None."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_end_date(raw):
    """`end_date` → aware datetime, mirroring the flock-create parser.

    A plain day (or naive datetime) is read as Asia/Tehran midnight — the same
    convention `POST /saloons/{id}/flocks/` uses for `start_date`, so a cycle
    opened with `2026-09-01` and closed with `2026-09-12` reports exactly those
    Tehran calendar days back.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    parsed = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        day = _parse_day(text)
        if day is None:
            return None
        return datetime.combine(day, dt_time.min, tzinfo=TEHRAN)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TEHRAN)
    return parsed


def _day_start(day: date) -> datetime:
    """Midnight Asia/Tehran of a calendar day (how a bare day is stored)."""
    return datetime.combine(day, dt_time.min, tzinfo=TEHRAN)


def _flock_in_saloon(flock_id, saloon):
    """(flock, error_response): 404 foreign/unknown, 400 wrong saloon.

    The reports wave set this precedent (a foreign flock aimed at your own
    saloon is a 400): the id belongs to the caller's org, so it is not an
    existence oracle — it is simply not the saloon's flock.
    """
    flock = FlockCycle.objects.filter(pk=flock_id, org_id=saloon.org_id).first()
    if flock is None:
        return None, _not_found("flock not found")
    if flock.saloon_id != saloon.pk:
        return None, _bad_request("flock does not belong to this saloon")
    return flock, None


# ---------------------------------------------------------------------------
# F1 — POST /farms/  (reached from api.farm_list, which owns the schema)
# ---------------------------------------------------------------------------


def create_farm(request):
    """Owner-exclusive farm creation; the org comes from the validated claim."""
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & OWNER_ROLES):
        return _forbidden("owner role required")

    payload = request.data if isinstance(request.data, dict) else {}
    name = _clean_name(payload.get("name"))
    if name is None:
        return _bad_request("invalid name")

    province, city, village, error = resolve_geography(payload)
    if error:
        return _bad_request(error)

    with transaction.atomic():
        farm = Farm.objects.create(
            org_id=org_id,
            name=name,
            province=province,
            city=city,
            village=village,
        )
        # The Flutter compat aliases resolve through legacy_slug_map; minting it
        # in the SAME transaction is what makes a new farm resolvable at once
        # (see apps/compat/legacy_slugs.py).
        mint_farm_slug(farm)
        _audit(
            request,
            ACTION_FARM_CREATED,
            org_id=org_id,
            farm=farm,
            details={"name": farm.name, "province_id": province.pk, "city_id": city.pk},
        )
    return Response(_farm_overview_payload(farm), status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# F2/F3 — PATCH|DELETE /farms/{farm_id}/
# ---------------------------------------------------------------------------


@extend_schema(
    methods=["PATCH"],
    tags=[TAG_FARMS],
    operation_id="update_farm",
    summary="F2 — rename a farm (owner)",
    description="""Name-only edit of one farm in the caller's org.

Geography is deliberately immutable here: moving a farm between provinces
re-homes every scope row and every regional rollup, so it is a migration, not a
rename. A foreign or unknown id is a 404; a farm outside the caller's scope
rows is a 403. Writes a `FARM_UPDATED` audit row in the same transaction and
returns the farm's `FarmOverview`.""",
    parameters=[
        OpenApiParameter(
            "farm_id", int, OpenApiParameter.PATH, description="Farm id (caller's org)."
        )
    ],
    request=FarmUpdateRequestSerializer,
    responses={
        200: FarmOverviewSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[OpenApiExample("Rename", request_only=True, value={"name": "Farm A (north)"})],
)
@extend_schema(
    methods=["DELETE"],
    tags=[TAG_FARMS],
    operation_id="delete_farm",
    summary="F3 — delete an empty farm (owner)",
    description="""Hard delete, **only while the farm has no saloons** — the RESTRICT
philosophy, never a cascade. A farm that still owns a saloon is a
`409 {"error": "farm not empty"}`; one that is still referenced by history
(alert instances, command logs) is a 409 with the relation named, never a 500.

The `legacy_slug_map` rows are left in place: an id is never re-issued, so the
alias stays pointed at a farm that no longer exists — deleting it would only
make an old alias look like a typo. Writes a `FARM_DELETED` audit row in the
same transaction.""",
    request=None,
    responses={
        200: FarmDeleteResponseSerializer,
        **error_responses(401, 403, 404),
        409: CONFLICT_RESPONSE,
    },
)
@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def farm_detail(request, farm_id: int):
    """PATCH (F2) and DELETE (F3) on ONE pattern — the path is shared, so a
    second `farms/<id>/` route would never be reached (Django matches the
    first pattern and answers 405). Schemas are attached per method above."""
    if request.method == "DELETE":
        return farm_delete(request, farm_id)
    return farm_update(request, farm_id)


def farm_update(request, farm_id: int):
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & OWNER_ROLES):
        return _forbidden("owner role required")

    farm = Farm.objects.filter(org_id=org_id, pk=farm_id).first()
    if farm is None:
        return _not_found("farm not found")
    if _scoped_out_farm(request, farm):
        return _forbidden()

    payload = request.data if isinstance(request.data, dict) else {}
    if "name" not in payload:
        return _bad_request("name required")
    name = _clean_name(payload.get("name"))
    if name is None:
        return _bad_request("invalid name")

    with transaction.atomic():
        farm.name = name
        farm.save(update_fields=["name"])
        _audit(
            request,
            ACTION_FARM_UPDATED,
            org_id=org_id,
            farm=farm,
            details={"name": farm.name},
        )
    return Response(_farm_overview_payload(farm))


def farm_delete(request, farm_id: int):
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & OWNER_ROLES):
        return _forbidden("owner role required")

    farm = Farm.objects.filter(org_id=org_id, pk=farm_id).first()
    if farm is None:
        return _not_found("farm not found")
    if _scoped_out_farm(request, farm):
        return _forbidden()

    guard = farm_delete_error(farm)
    if guard:
        return _conflict(guard)

    farm_pk, farm_name = farm.pk, farm.name
    refused = delete_row(farm)
    if refused:
        return _conflict(refused)
    # Audited AFTER the delete: the *_DELETED line names the farm only in
    # `details` (the row is already gone — a SET_NULL FK could not point at
    # it anyway, BUG-10/BUG-18) — and a REFUSED delete leaves no line at
    # all (it returns before this point).
    _audit(
        request,
        ACTION_FARM_DELETED,
        org_id=org_id,
        details={"farm_id": farm_pk, "name": farm_name},
    )
    return Response({"farm_id": farm_id, "deleted": True})


# ---------------------------------------------------------------------------
# F4 — POST /farms/{farm_id}/saloons/
# ---------------------------------------------------------------------------


@extend_schema(
    tags=[TAG_FARMS],
    operation_id="create_saloon",
    summary="F4 — add a saloon to a farm (manager+)",
    description="""Creates one saloon under a farm of the caller's org; positions start
EMPTY by design (F7 adds them one at a time, so an onboarding mistake is one
position, not a seeded topology nobody asked for).

The saloon inherits the farm's org and gets its `device-<id>` compat slug in
the same transaction. Foreign/unknown farm → 404; a farm outside the caller's
scope rows → 403. Returns the new `SaloonDetail` (201) and writes a
`SALOON_CREATED` audit row.""",
    parameters=[
        OpenApiParameter(
            "farm_id", int, OpenApiParameter.PATH, description="Farm id (caller's org)."
        )
    ],
    request=SaloonCreateRequestSerializer,
    responses={
        201: SaloonDetailSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[OpenApiExample("Add saloon", request_only=True, value={"name": "Saloon 3"})],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def saloon_create(request, farm_id: int):
    org_id = _org_id(request)
    if org_id is None or not _may_write_farm(request, farm_id):
        return _forbidden(
            "manager role required (or an installer session for this farm)"
        )

    farm = Farm.objects.filter(org_id=org_id, pk=farm_id).first()
    if farm is None:
        return _not_found("farm not found")
    if _scoped_out_farm(request, farm):
        return _forbidden()

    payload = request.data if isinstance(request.data, dict) else {}
    name = _clean_name(payload.get("name"))
    if name is None:
        return _bad_request("invalid name")

    with transaction.atomic():
        saloon = Saloon.objects.create(org_id=farm.org_id, farm=farm, name=name)
        mint_saloon_slug(saloon)
        _audit(
            request,
            ACTION_SALOON_CREATED,
            org_id=farm.org_id,
            farm=farm,
            saloon=saloon,
            details={"name": saloon.name},
        )
    return Response(
        _saloon_detail_payload(saloon), status=status.HTTP_201_CREATED
    )


# ---------------------------------------------------------------------------
# F5/F6 — PATCH|DELETE /saloons/{saloon_id}/  (reached from api.saloon_detail)
# ---------------------------------------------------------------------------


def update_saloon(request, saloon_id: int):
    """PATCH half of the saloon detail route (manager+); schema lives in api.py."""
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & MANAGER_ROLES):
        return _forbidden("manager role required")

    saloon = (
        Saloon.objects.select_related("farm", "farm__province", "farm__city", "farm__village")
        .filter(org_id=org_id, pk=saloon_id)
        .first()
    )
    if saloon is None:
        return _not_found("saloon not found")
    if _scoped_out_saloon(request, saloon):
        return _forbidden()

    payload = request.data if isinstance(request.data, dict) else {}
    if "name" not in payload:
        return _bad_request("name required")
    name = _clean_name(payload.get("name"))
    if name is None:
        return _bad_request("invalid name")

    with transaction.atomic():
        saloon.name = name
        saloon.save(update_fields=["name"])
        _audit(
            request,
            ACTION_SALOON_UPDATED,
            org_id=org_id,
            farm=saloon.farm,
            saloon=saloon,
            details={"name": saloon.name},
        )
    return Response(_saloon_detail_payload(saloon))


def delete_saloon(request, saloon_id: int):
    """DELETE half of the saloon detail route (manager+); schema lives in api.py."""
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & MANAGER_ROLES):
        return _forbidden("manager role required")

    saloon = Saloon.objects.select_related("farm").filter(org_id=org_id, pk=saloon_id).first()
    if saloon is None:
        return _not_found("saloon not found")
    if _scoped_out_saloon(request, saloon):
        return _forbidden()

    guard = saloon_delete_error(saloon)
    if guard:
        return _conflict(guard)

    farm, name = saloon.farm, saloon.name
    refused = delete_row(saloon)
    if refused:
        return _conflict(refused)
    _audit(
        request,
        ACTION_SALOON_DELETED,
        org_id=org_id,
        farm=farm,
        details={"saloon_id": saloon_id, "name": name},
    )
    return Response({"saloon_id": str(saloon_id), "deleted": True})


# ---------------------------------------------------------------------------
# F7/F8 — positions
# ---------------------------------------------------------------------------


def _position_out(row: SaloonPosition) -> dict:
    return {
        "position_id": row.pk,
        "saloon_id": str(row.saloon_id),
        "label": row.label,
        "created_by": row.created_by_id,
    }


@extend_schema(
    tags=[TAG_FARMS],
    operation_id="create_saloon_position",
    summary="F7 — add a physical position to a saloon (manager+)",
    description="""Creates one named position ("North Wall", …) under a saloon of the
caller's org. The sensor hierarchy is saloon → position → sensor slot, so this
is the step that must exist before a slot can be bound.

`UNIQUE(saloon_id, label)` stands: a duplicate label is a `409`, pre-checked and
backed by the constraint (the catch is what makes a concurrent double-submit
behave). Foreign/unknown saloon → 404; outside the caller's scope → 403.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        )
    ],
    request=PositionCreateRequestSerializer,
    responses={
        201: PositionItemSerializer,
        **error_responses(400, 401, 403, 404),
        409: CONFLICT_RESPONSE,
    },
    examples=[OpenApiExample("Add position", request_only=True, value={"label": "North Wall"})],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def position_create(request, saloon_id: int):
    org_id = _org_id(request)
    if org_id is None or not (
        (_roles(request) & MANAGER_ROLES)
        or _installer_session_saloon(request, saloon_id)
    ):
        return _forbidden(
            "manager role required (or an installer session for this farm)"
        )

    saloon = Saloon.objects.filter(org_id=org_id, pk=saloon_id).first()
    if saloon is None:
        return _not_found("saloon not found")
    if _scoped_out_saloon(request, saloon):
        return _forbidden()

    payload = request.data if isinstance(request.data, dict) else {}
    label = str(payload.get("label") or "").strip()
    if not label or len(label) > MAX_LABEL:
        return _bad_request("invalid label")
    if SaloonPosition.objects.filter(saloon=saloon, label=label).exists():
        return _conflict("a position with this label already exists")

    try:
        with transaction.atomic():
            row = SaloonPosition.objects.create(
                saloon=saloon,
                org_id=org_id,
                label=label,
                created_by=request.user,
            )
            _audit(
                request,
                ACTION_POSITION_CREATED,
                org_id=org_id,
                farm=saloon.farm,
                saloon=saloon,
                details={"position_id": row.pk, "label": row.label},
            )
    except IntegrityError:
        return _conflict("a position with this label already exists")
    return Response(_position_out(row), status=status.HTTP_201_CREATED)


@extend_schema(
    tags=[TAG_FARMS],
    operation_id="update_saloon_position",
    summary="F8 — rename a position (manager+)",
    description="""Renames one position. Sensor bindings follow automatically — the
mapping rows reference the position by id, so nothing is re-bound and no
telemetry history moves.

Label-only: the follow-through draft also listed an `is_active` toggle here,
but `saloon_positions` has no such column and the core schema is frozen, so the
erratum is recorded in the spec instead of an `ALTER` (see
`docs/api-followthrough-v1.md` F8). A duplicate label is a 409 like F7.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        ),
        OpenApiParameter(
            "position_id", int, OpenApiParameter.PATH, description="Position id (that saloon)."
        ),
    ],
    request=PositionUpdateRequestSerializer,
    responses={
        200: PositionItemSerializer,
        **error_responses(400, 401, 403, 404),
        409: CONFLICT_RESPONSE,
    },
    examples=[OpenApiExample("Rename", request_only=True, value={"label": "North Wall (upper)"})],
)
@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def position_update(request, saloon_id: int, position_id: int):
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & MANAGER_ROLES):
        return _forbidden("manager role required")

    saloon = Saloon.objects.filter(org_id=org_id, pk=saloon_id).first()
    if saloon is None:
        return _not_found("saloon not found")
    if _scoped_out_saloon(request, saloon):
        return _forbidden()

    row = SaloonPosition.objects.filter(pk=position_id, saloon=saloon).first()
    if row is None:
        return _not_found("position not found")

    payload = request.data if isinstance(request.data, dict) else {}
    if "label" not in payload:
        return _bad_request("label required")
    label = str(payload.get("label") or "").strip()
    if not label or len(label) > MAX_LABEL:
        return _bad_request("invalid label")
    if SaloonPosition.objects.filter(saloon=saloon, label=label).exclude(pk=row.pk).exists():
        return _conflict("a position with this label already exists")

    try:
        with transaction.atomic():
            row.label = label
            row.save(update_fields=["label"])
            _audit(
                request,
                ACTION_POSITION_UPDATED,
                org_id=org_id,
                farm=saloon.farm,
                saloon=saloon,
                details={"position_id": row.pk, "label": row.label},
            )
    except IntegrityError:
        return _conflict("a position with this label already exists")
    return Response(_position_out(row))


# ---------------------------------------------------------------------------
# F7b — PATCH|DELETE /saloons/{saloon_id}/sensor-slots/{slot_id}/
# ---------------------------------------------------------------------------


@extend_schema(
    methods=["PATCH"],
    tags=[TAG_FARMS],
    operation_id="update_saloon_sensor_slot",
    summary="Retire, re-arm or re-scope a sensor slot (manager+)",
    description="""The write surface `saloon_sensor_slots.is_active` never had.

Before this endpoint the column existed, the LOS watchdog read it, the OpenAPI
documented it — and nothing in the product could write it. An operator who
pulled a sensor had no supported way to say so, so the saloon stayed flagged
"lost" forever (the D20 orphan-alert family, reached from the other side).

`is_active=false` retires the slot. The LOS watchdog closes the slot's open
LOS row on its next sweep — the same path a retired saloon takes, no special
case here. Re-arming (`true`) makes the slot eligible for LOS again from the
next sweep.

`allowed_metric_keys` re-scopes what the slot may report; the list is replaced
wholesale, never merged. An empty body is a 400 rather than a silent no-op.
Writes `SENSOR_SLOT_UPDATED` with the changed fields.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        ),
        OpenApiParameter(
            "slot_id", str, OpenApiParameter.PATH, description="Sensor slot id (that saloon)."
        ),
    ],
    request=SensorSlotUpdateRequestSerializer,
    responses={
        200: SensorSlotResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample("Retire the slot", request_only=True, value={"is_active": False}),
        OpenApiExample(
            "Re-scope the metrics",
            request_only=True,
            value={"allowed_metric_keys": ["temp", "humidity"]},
        ),
    ],
)
@extend_schema(
    methods=["DELETE"],
    tags=[TAG_FARMS],
    operation_id="retire_saloon_sensor_slot",
    summary="Retire a sensor slot (manager+)",
    description="""The DELETE verb is the soft-off: `is_active=false`, nothing removed.

The row is never hard-deleted — `hardware_registry` install history and every
telemetry row point at the slot, and a gap in that history is worse than a
retired row. Idempotent: retiring an already-retired slot answers 200 with
`idempotent: true` instead of a 404, so a retried operator action converges.

Writes `SENSOR_SLOT_RETIRED`.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        ),
        OpenApiParameter(
            "slot_id", str, OpenApiParameter.PATH, description="Sensor slot id (that saloon)."
        ),
    ],
    request=None,
    responses={
        200: SensorSlotResponseSerializer,
        **error_responses(401, 403, 404),
    },
)
@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def saloon_sensor_slot_detail(request, saloon_id: int, slot_id: str):
    """PATCH edits a slot; DELETE retires it (soft — history stays intact)."""
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & MANAGER_ROLES):
        return _forbidden("manager role required")

    saloon = Saloon.objects.filter(org_id=org_id, pk=saloon_id).first()
    if saloon is None:
        return _not_found("saloon not found")
    if _scoped_out_saloon(request, saloon):
        return _forbidden()

    # Scoped to THIS saloon on purpose: slot ids are a global primary key, so a
    # foreign saloon's slot must be the same 404 as an unknown one (no oracle).
    row = SaloonSensorSlot.objects.filter(pk=slot_id, saloon=saloon).first()
    if row is None:
        return _not_found("sensor slot not found")

    if request.method == "DELETE":
        if not row.is_active:
            out = {**_sensor_slot_out(row), "retired": True, "idempotent": True}
            return Response(out)
        with transaction.atomic():
            row.is_active = False
            row.save(update_fields=["is_active"])
            _audit(
                request,
                ACTION_SENSOR_SLOT_RETIRED,
                org_id=org_id,
                farm=saloon.farm,
                saloon=saloon,
                details={"slot_id": row.slot_id, "via": "DELETE"},
            )
        return Response({**_sensor_slot_out(row), "retired": True})

    payload = request.data if isinstance(request.data, dict) else {}
    changed = []
    if "is_active" in payload:
        value = payload.get("is_active")
        if not isinstance(value, bool):
            return _bad_request("is_active must be a boolean")
        changed.append("is_active")
    if "allowed_metric_keys" in payload:
        metrics = payload.get("allowed_metric_keys")
        if not isinstance(metrics, list) or any(not isinstance(m, str) for m in metrics):
            return _bad_request("allowed_metric_keys must be a list of strings")
        changed.append("allowed_metric_keys")
    if not changed:
        return _bad_request("nothing to update (is_active or allowed_metric_keys required)")

    with transaction.atomic():
        if "is_active" in payload:
            row.is_active = payload["is_active"]
        if "allowed_metric_keys" in payload:
            row.allowed_metric_keys = payload["allowed_metric_keys"]
        row.save(update_fields=changed)
        _audit(
            request,
            ACTION_SENSOR_SLOT_UPDATED,
            org_id=org_id,
            farm=saloon.farm,
            saloon=saloon,
            details={
                "slot_id": row.slot_id,
                "changed": changed,
                "is_active": row.is_active,
            },
        )
    return Response(_sensor_slot_out(row))


# ---------------------------------------------------------------------------
# F14 — PATCH /saloons/{saloon_id}/flocks/{flock_id}/close/
# ---------------------------------------------------------------------------


@extend_schema(
    tags=[TAG_FARMS],
    operation_id="close_flock_cycle",
    summary="F14 — close the active flock cycle (engineer+)",
    description="""Sets the cycle's `end_date` — the write this table never had, and
the one every downstream reader waits for: the KPI windows, the quality
service's per-cycle verdicts and the threshold hash all key off
`end_date IS NULL` meaning "active".

Only the saloon's ACTIVE cycle can be closed (`end_date` already set → 400
`no active flock`), and `end_date` may not precede `start_date`. The save fires
the existing `flock_cycles.post_save` fast path, which re-arms the saloon's
threshold hash immediately — a cycle that ends mid-day must not leave the
previous age band armed.

**Snapshot finalization is NOT part of this endpoint.** The follow-through
draft mentioned it; the `flock_daily_snapshots` table and the aggregates module
that would write it exist in no wave yet, so promising it here would be a
promise the code cannot keep (see the erratum in `docs/api-followthrough-v1.md`
F14).""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        ),
        OpenApiParameter(
            "flock_id", int, OpenApiParameter.PATH, description="Flock cycle id (that saloon)."
        ),
    ],
    request=FlockCloseRequestSerializer,
    responses={
        200: FlockCloseResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample("Close the cycle", request_only=True, value={"end_date": "2026-09-12"})
    ],
)
@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def flock_close(request, saloon_id: int, flock_id: int):
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & WRITE_ROLES):
        return _forbidden("engineer role required")

    saloon = Saloon.objects.filter(org_id=org_id, pk=saloon_id).first()
    if saloon is None:
        return _not_found("saloon not found")
    if _scoped_out_saloon(request, saloon):
        return _forbidden()

    flock, error = _flock_in_saloon(flock_id, saloon)
    if error is not None:
        return error
    if flock.end_date is not None:
        return _bad_request("no active flock")

    payload = request.data if isinstance(request.data, dict) else {}
    end_date = _parse_end_date(payload.get("end_date"))
    if end_date is None:
        return _bad_request("end_date required (YYYY-MM-DD or ISO datetime)")
    if end_date < flock.start_date:
        return _bad_request("end_date must be ≥ start_date")

    with transaction.atomic():
        flock.end_date = end_date
        flock.save(update_fields=["end_date"])
        _audit(
            request,
            ACTION_FLOCK_CLOSED,
            org_id=org_id,
            farm=saloon.farm,
            saloon=saloon,
            details={
                "flock_id": flock.pk,
                "end_date": _flock_date(flock.end_date),
            },
        )
    return Response(
        {
            "flock_id": flock.pk,
            "saloon_id": str(saloon.pk),
            "end_date": _flock_date(flock.end_date),
            "status": "CLOSED",
        }
    )


# ---------------------------------------------------------------------------
# F15 — POST /saloons/{saloon_id}/flocks/{flock_id}/mortality/
# ---------------------------------------------------------------------------


@extend_schema(
    tags=[TAG_FARMS],
    operation_id="record_mortality",
    summary="F15 — record deaths directly (engineer+)",
    description="""The writer `mortality_records` never had. `GET /saloons/{id}/kpis/`
computes `mortality_pct` and the daily series from THIS table, while the daily
report stores its own `mortality_count` — before this endpoint nothing in the
API could write a mortality row, so the farmer-facing mortality figure could
only ever read zero.

Direct entry sits BESIDE the report counts (the spec's wording), so both
sources stay honest: this endpoint never rewrites a report and a report never
writes here.

`occurred_on` is a Tehran calendar day, stored at 00:00 Asia/Tehran (the same
day boundary the KPI groups by). It may not be in the future nor precede the
cycle's start. `count` is ≥ 1. `cause` is an additive extension of the draft —
the column exists and was otherwise unreachable.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        ),
        OpenApiParameter(
            "flock_id", int, OpenApiParameter.PATH, description="Flock cycle id (that saloon)."
        ),
    ],
    request=MortalityCreateRequestSerializer,
    responses={
        201: MortalityResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Fifteen deaths",
            request_only=True,
            value={
                "occurred_on": "2026-09-11",
                "count": 15,
                "cause": "heat_stress",
                "note": "Heat spike during the fan outage",
            },
        )
    ],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def flock_mortality(request, saloon_id: int, flock_id: int):
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & WRITE_ROLES):
        return _forbidden("engineer role required")

    saloon = Saloon.objects.filter(org_id=org_id, pk=saloon_id).first()
    if saloon is None:
        return _not_found("saloon not found")
    if _scoped_out_saloon(request, saloon):
        return _forbidden()

    flock, error = _flock_in_saloon(flock_id, saloon)
    if error is not None:
        return error

    payload = request.data if isinstance(request.data, dict) else {}
    day = _parse_day(payload.get("occurred_on"))
    if day is None:
        return _bad_request("occurred_on required (YYYY-MM-DD)")
    try:
        count = int(payload.get("count"))
    except (TypeError, ValueError):
        return _bad_request("count required and must be an integer")
    if count < 1:
        return _bad_request("count must be ≥ 1")

    today = datetime.now(TEHRAN).date()
    if day > today:
        return _bad_request("occurred_on may not be in the future")
    if day < flock.start_date.astimezone(TEHRAN).date():
        return _bad_request("occurred_on may not precede the cycle start")

    cause = str(payload.get("cause") or "").strip() or None
    if cause is not None and len(cause) > 100:
        return _bad_request("cause must be ≤ 100 characters")
    notes = str(payload.get("note") or payload.get("notes") or "").strip() or None

    with transaction.atomic():
        row = MortalityRecord.objects.create(
            org_id=org_id,
            flock=flock,
            saloon=saloon,
            recorded_by=request.user,
            count=count,
            cause=cause,
            notes=notes,
            occurred_at=_day_start(day),
        )
        _audit(
            request,
            ACTION_MORTALITY_RECORDED,
            org_id=org_id,
            farm=saloon.farm,
            saloon=saloon,
            details={"flock_id": flock.pk, "count": count, "occurred_on": day.isoformat()},
        )
    return Response(
        {
            "mortality_id": row.pk,
            "flock_id": flock.pk,
            "saloon_id": str(saloon.pk),
            "count": count,
            "occurred_on": day.isoformat(),
            "recorded_by": request.user.pk,
            "cause": cause,
            "notes": notes,
        },
        status=status.HTTP_201_CREATED,
    )


# ---------------------------------------------------------------------------
# F10 — GET /overview/  (the owner home rollup)
# ---------------------------------------------------------------------------


@extend_schema(
    tags=[TAG_FARMS],
    operation_id="get_overview",
    summary="F10 — org home rollup (any member)",
    description="""Every farm of the caller's org with its saloon count, unresolved-alert
count and highest active tier, plus org totals — the missing multi-farm home
view (`GET /farms/{id}/overview/` answers one farm at a time).

Aggregates are computed in bulk (no per-farm query) and RLS/scope narrow them:
a member with `account_role_scopes` rows sees only their farms and their
totals. `highest_tier` is null when nothing is unresolved, never 0 — an empty
farm must not read as "tier zero".""",
    request=None,
    responses={200: OverviewResponseSerializer, **error_responses(401)},
    examples=[
        OpenApiExample(
            "Two farms",
            response_only=True,
            value={
                "farms": [
                    {
                        "farm_id": 1,
                        "name": "Farm A",
                        "saloon_count": 3,
                        "active_alert_count": 2,
                        "highest_tier": 2,
                    }
                ],
                "totals": {
                    "farm_count": 1,
                    "saloon_count": 3,
                    "active_alert_count": 2,
                    "highest_tier": 2,
                },
            },
        )
    ],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def overview(request):
    org_id = _org_id(request)
    if org_id is None:
        return _bad_request("organization context required")

    farms = Farm.objects.filter(org_id=org_id).order_by("id")
    scoped = scoped_farms(request.user, org_id)
    if scoped is not None:
        farms = farms.filter(pk__in=scoped.values("pk"))
    farms = list(farms)

    saloons = Saloon.objects.filter(org_id=org_id)
    scoped_sal = scoped_saloons(request.user, org_id)
    if scoped_sal is not None:
        saloons = saloons.filter(pk__in=scoped_sal.values("pk"))

    saloon_counts = {
        row["farm_id"]: row["n"]
        for row in saloons.values("farm_id").annotate(n=Count("id"))
    }
    # One aggregate for every saloon the caller can see, grouped by farm.
    alert_rows = (
        AlertInstance.objects.filter(org_id=org_id, resolved_at__isnull=True)
        .filter(saloon__in=saloons.values("pk"))
        .values("farm_id")
        .annotate(n=Count("id"), top=Max("highest_tier_reached"))
    )
    alert_stats = {row["farm_id"]: row for row in alert_rows}

    out = []
    for farm in farms:
        stats = alert_stats.get(farm.pk) or {"n": 0, "top": None}
        out.append(
            {
                "farm_id": farm.pk,
                "name": farm.name,
                "saloon_count": saloon_counts.get(farm.pk, 0),
                "active_alert_count": stats["n"],
                "highest_tier": stats["top"],
            }
        )

    return Response(
        {
            "farms": out,
            "totals": {
                "farm_count": len(out),
                "saloon_count": sum(row["saloon_count"] for row in out),
                "active_alert_count": sum(row["active_alert_count"] for row in out),
                "highest_tier": max(
                    (row["highest_tier"] for row in out if row["highest_tier"] is not None),
                    default=None,
                ),
            },
        }
    )
