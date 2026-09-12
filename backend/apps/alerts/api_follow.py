"""alerts follow-through API — org alert feed + maintenance-mute windows (v1.7.2).

Spec: `docs/api-followthrough-v1.md` F9, F11, F12, F13. Additive-only: both
surfaces read/write frozen tables, no migration.

  F9  GET    /alerts/                  engineer+   the org-wide feed
  F11 GET    /maintenance-mutes/       manager+    first READ path for the table
  F12 POST   /maintenance-mutes/       manager+    first WRITE path, ever
  F13 DELETE /maintenance-mutes/{id}/  manager+    revoke early

TWO things this module changes about the world, both deliberate and recorded:

1. `MaintenanceMute`'s docstring said "Managed via Django Admin in v1 — no REST
   endpoint". No admin registration ever shipped, so the feature was reachable
   only from tests and the ORM — the engine read a table nothing could write.
   The docstring is updated in the same commit as this file.
2. The alert feed is the org-wide sibling of `GET /saloons/{id}/alerts/`: same
   row shape (one `_alert_out`), same "a typo is a 400, never a silent widen"
   rule, now with farm/tier/metric filters and pagination.

Velocity is untouched: mutes suppress threshold/duration alerts only, and the
Decision 10 invariant (NH₃/temp velocity always pages) lives in the evaluation
path — this surface cannot reach it.

فارسی: فهرستِ سازمانیِ هشدارها و مسیرِ نوشتنِ پنجره‌های بی‌صداکننده؛ سرعتِ اضطراری
هرگز بی‌صدا نمی‌شود.
"""

from datetime import timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims, scoped_saloons
from apps.alerts import services_follow as svc
from apps.alerts.models import AlertInstance, MaintenanceMute
from apps.alerts.serializers import METRIC_TYPE_CHOICES
from apps.alerts.serializers_follow import (
    AlertFeedResponseSerializer,
    MuteCreateRequestSerializer,
    MuteListResponseSerializer,
    MuteItemSerializer,
    MuteRevokeResponseSerializer,
)
from apps.alerts.views import _alert_out
from apps.auditing.models import AccountActivityLog
from apps.common.schemas import CONFLICT_RESPONSE, TAG_ALERTS, error_responses
from apps.farms.models import Saloon
from apps.monitoring.ops_services import InvalidPagination, parse_iso_z, parse_pagination

FEED_ROLES = {"engineer", "manager", "owner"}
MANAGER_ROLES = {"manager", "owner"}
ACTIVE_STATUSES = ("ACTIVE", "ALL")
ACTION_MUTE_CREATED = "MUTE_CREATED"
ACTION_MUTE_REVOKED = "MUTE_REVOKED"


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


def _scoped_out(request, saloon) -> bool:
    """True when the caller's scope rows exclude this saloon (403, never a widen)."""
    scoped = scoped_saloons(request.user, saloon.org_id)
    return scoped is not None and not scoped.filter(pk=saloon.pk).exists()


def _mute_out(row: MaintenanceMute, *, now=None) -> dict:
    now = now or timezone.now()
    return {
        "mute_id": row.pk,
        "saloon_id": str(row.saloon_id),
        "farm_id": row.saloon.farm_id if row.saloon_id else None,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "reason": row.reason,
        # The ENGINE's predicate (services/mutes.active_maintenance_mute), so a
        # row reads active here exactly when it suppresses an alert.
        "active": bool(row.starts_at and row.ends_at and row.starts_at <= now < row.ends_at),
        "created_by": row.created_by_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------------------
# F9 — GET /alerts/
# ---------------------------------------------------------------------------


@extend_schema(
    tags=[TAG_ALERTS],
    operation_id="list_org_alerts",
    summary="F9 — org-wide alert feed (engineer+)",
    description="""The missing org feed: every alert of the caller's org, newest first,
with `{results, count}` pagination. `GET /saloons/{id}/alerts/` answers one
saloon at a time; this answers "what is on fire right now" for the whole farm
or the whole org.

Filters: `farm_id`, `saloon_id`, `status` (`ACTIVE` default / `ALL`), `tier`
(1-3 on `highest_tier_reached`), `metric` (metric key). Each is validated — an
unknown tier or metric key is a 400, because a typo that silently narrows a
feed reads exactly like "nothing is wrong".

Scope rows narrow the result set like every other read surface (M2-06): a
member with `account_role_scopes` rows sees only their saloons.""",
    parameters=[
        OpenApiParameter(
            "limit", int, OpenApiParameter.QUERY, required=False,
            description="Page size (default 50, max 500).",
        ),
        OpenApiParameter(
            "offset", int, OpenApiParameter.QUERY, required=False,
            description="Page start (default 0).",
        ),
        OpenApiParameter(
            "farm_id", int, OpenApiParameter.QUERY, required=False,
            description="Restrict to one farm.",
        ),
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.QUERY, required=False,
            description="Restrict to one saloon.",
        ),
        OpenApiParameter(
            "status", str, OpenApiParameter.QUERY, required=False, enum=list(ACTIVE_STATUSES),
            description="ACTIVE (default) skips resolved rows; ALL returns the history.",
        ),
        OpenApiParameter(
            "tier", int, OpenApiParameter.QUERY, required=False,
            description="Exact highest escalation tier reached (1-3).",
        ),
        OpenApiParameter(
            "metric", str, OpenApiParameter.QUERY, required=False,
            description="Metric key, e.g. temp, nh3, co2.",
        ),
    ],
    request=None,
    responses={
        200: AlertFeedResponseSerializer,
        **error_responses(400, 401, 403),
    },
    examples=[
        OpenApiExample(
            "Active Tier-2 ammonia alerts",
            response_only=True,
            value={
                "results": [
                    {
                        "alert_id": "6b1f0cbe-2f2f-4a3d-9d1a-6f0b7c1e5a11",
                        "metric_type": "nh3",
                        "current_status": "ACTIVE_TIER_2",
                        "current_value": 27.5,
                        "source": "REALTIME",
                        "triggered_at": "2026-09-11T21:02:00Z",
                        "resolved_at": None,
                        "highest_tier_reached": 2,
                    }
                ],
                "count": 1,
            },
        )
    ],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def alert_feed(request):
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & FEED_ROLES):
        return _forbidden("engineer role required")

    try:
        limit, offset = parse_pagination(request)
    except InvalidPagination:
        return _bad_request("invalid pagination")

    qs = AlertInstance.objects.filter(org_id=org_id)

    wanted = str(request.query_params.get("status", "ACTIVE")).strip().upper()
    if wanted == "ACTIVE":
        qs = qs.filter(resolved_at__isnull=True)
    elif wanted not in ACTIVE_STATUSES:
        return _bad_request("status must be ACTIVE or ALL")

    raw_saloon = str(request.query_params.get("saloon_id", "")).strip()
    if raw_saloon:
        try:
            qs = qs.filter(saloon_id=int(raw_saloon))
        except ValueError:
            return _bad_request("saloon_id must be an integer")

    raw_farm = str(request.query_params.get("farm_id", "")).strip()
    if raw_farm:
        try:
            qs = qs.filter(farm_id=int(raw_farm))
        except ValueError:
            return _bad_request("farm_id must be an integer")

    raw_tier = str(request.query_params.get("tier", "")).strip()
    if raw_tier:
        try:
            tier = int(raw_tier)
        except ValueError:
            return _bad_request("tier must be 1, 2 or 3")
        if tier not in (1, 2, 3):
            return _bad_request("tier must be 1, 2 or 3")
        qs = qs.filter(highest_tier_reached=tier)

    raw_metric = str(request.query_params.get("metric", "")).strip()
    if raw_metric:
        if raw_metric not in METRIC_TYPE_CHOICES:
            return _bad_request("unknown metric")
        qs = qs.filter(metric_type=raw_metric)

    scoped = scoped_saloons(request.user, org_id)
    if scoped is not None:
        qs = qs.filter(saloon_id__in=scoped.values("pk"))

    qs = qs.order_by("-triggered_at")
    total = qs.count()
    rows = qs[offset : offset + limit]
    return Response({"results": [_alert_out(row) for row in rows], "count": total})


# ---------------------------------------------------------------------------
# F11/F12 — GET|POST /maintenance-mutes/
# ---------------------------------------------------------------------------


@extend_schema(
    methods=["GET"],
    tags=[TAG_ALERTS],
    operation_id="list_maintenance_mutes",
    summary="F11 — list maintenance-mute windows (manager+)",
    description="""First READ path for `maintenance_mutes`. `?active_only=true`
(default) keeps only windows the engine would honour right now
(`starts_at ≤ now < ends_at`); `?active_only=false` returns the history.
`?saloon_id=` narrows to one saloon.

Rows are newest-first with `{results, count}`. A foreign saloon filter cannot
leak another org's rows: the set is always claim-org scoped, then narrowed by
the caller's `account_role_scopes` rows.""",
    parameters=[
        OpenApiParameter(
            "limit", int, OpenApiParameter.QUERY, required=False,
            description="Page size (default 50, max 500).",
        ),
        OpenApiParameter(
            "offset", int, OpenApiParameter.QUERY, required=False,
            description="Page start (default 0).",
        ),
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.QUERY, required=False,
            description="Restrict to one saloon.",
        ),
        OpenApiParameter(
            "active_only", bool, OpenApiParameter.QUERY, required=False,
            description="Default true — only windows active right now.",
        ),
    ],
    request=None,
    responses={200: MuteListResponseSerializer, **error_responses(400, 401, 403)},
)
@extend_schema(
    methods=["POST"],
    tags=[TAG_ALERTS],
    operation_id="create_maintenance_mute",
    summary="F12 — open a maintenance-mute window (manager+)",
    description="""First WRITE path for `maintenance_mutes`. While the window covers
"now", threshold-crossing and duration-filter alerts for that saloon are
SUPPRESSED — readings still log, and Tier-1 velocity (NH₃/temp) still pages
(Decision 10 is enforced in the evaluation path and cannot be reached from
here).

Rules: `ends_at` after `starts_at`, window ≤ 72 h, and no overlap with another
window of the same saloon (abutting windows are allowed — back-to-back
maintenance is a normal schedule). A window that is already over is refused; one
that has started but not ended is accepted (opening a mute mid-job is the normal
case). Writes a `MUTE_CREATED` audit row in the same transaction.""",
    request=MuteCreateRequestSerializer,
    responses={
        201: MuteItemSerializer,
        **error_responses(400, 401, 403, 404),
        409: CONFLICT_RESPONSE,
    },
    examples=[
        OpenApiExample(
            "Fan replacement",
            request_only=True,
            value={
                "saloon_id": 7,
                "starts_at": "2026-09-12T06:00:00Z",
                "ends_at": "2026-09-12T12:00:00Z",
                "reason": "Exhaust fan replacement",
            },
        )
    ],
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def maintenance_mutes(request):
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & MANAGER_ROLES):
        return _forbidden("manager role required")

    if request.method == "POST":
        return _mute_create(request, org_id)

    try:
        limit, offset = parse_pagination(request)
    except InvalidPagination:
        return _bad_request("invalid pagination")

    qs = MaintenanceMute.objects.select_related("saloon").filter(org_id=org_id)
    raw_saloon = str(request.query_params.get("saloon_id", "")).strip()
    if raw_saloon:
        try:
            qs = qs.filter(saloon_id=int(raw_saloon))
        except ValueError:
            return _bad_request("saloon_id must be an integer")

    raw_active = str(request.query_params.get("active_only", "true")).strip().lower()
    if raw_active in ("true", "1", "yes"):
        now = timezone.now()
        qs = qs.filter(starts_at__lte=now, ends_at__gt=now)
    elif raw_active not in ("false", "0", "no"):
        return _bad_request("active_only must be true or false")

    scoped = scoped_saloons(request.user, org_id)
    if scoped is not None:
        qs = qs.filter(saloon_id__in=scoped.values("pk"))

    qs = qs.order_by("-starts_at")
    total = qs.count()
    now = timezone.now()
    rows = qs[offset : offset + limit]
    return Response(
        {"results": [_mute_out(row, now=now) for row in rows], "count": total}
    )


def _mute_create(request, org_id):
    payload = request.data if isinstance(request.data, dict) else {}

    try:
        saloon_id = int(payload.get("saloon_id"))
    except (TypeError, ValueError):
        return _bad_request("saloon_id required and must be an integer")
    saloon = Saloon.objects.filter(org_id=org_id, pk=saloon_id).first()
    if saloon is None:
        return _not_found("saloon not found")
    if _scoped_out(request, saloon):
        return _forbidden()

    starts_at = parse_iso_z(str(payload.get("starts_at") or "").strip())
    ends_at = parse_iso_z(str(payload.get("ends_at") or "").strip())
    if starts_at is None:
        return _bad_request("starts_at required (ISO-8601)")
    if ends_at is None:
        return _bad_request("ends_at required (ISO-8601)")
    # A naive stamp would reach Postgres as a "naive datetime" the ORM warns
    # about; read it as UTC instead of guessing a zone (the engine compares
    # instants, and UTC is the contract's own convention elsewhere).
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=dt_timezone.utc)
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=dt_timezone.utc)

    reason = str(payload.get("reason") or "").strip()
    if not reason or len(reason) > svc.MAX_REASON:
        return _bad_request("invalid reason")

    error = svc.window_error(starts_at, ends_at)
    if error:
        return _bad_request(error)
    if svc.overlapping_mute(saloon.pk, starts_at, ends_at) is not None:
        return _conflict("mute window overlaps an existing window for this saloon")

    with transaction.atomic():
        row = MaintenanceMute.objects.create(
            org_id=org_id,
            saloon=saloon,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=reason,
            created_by=request.user,
        )
        AccountActivityLog.objects.create(
            actor=request.user,
            org_id=org_id,
            action=ACTION_MUTE_CREATED,
            farm=saloon.farm,
            saloon=saloon,
            ip_address=request.META.get("REMOTE_ADDR"),
            details={
                "mute_id": row.pk,
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "reason": reason,
            },
        )
    return Response(_mute_out(row), status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# F13 — DELETE /maintenance-mutes/{mute_id}/
# ---------------------------------------------------------------------------


@extend_schema(
    tags=[TAG_ALERTS],
    operation_id="revoke_maintenance_mute",
    summary="F13 — revoke a mute window (manager+)",
    description="""Ends a window early so alerting resumes at once.

Two honest branches, because `maintenance_mutes` carries a CHECK constraint
(`ends_at > starts_at`) that a careless revocation would violate:

* the window is RUNNING → `ends_at` is shortened to the revocation instant, the
  row and its history stay;
* the window has NOT STARTED → the row is removed (it suppressed nothing, so
  there is no history to keep and no valid way to move a future window into the
  past without falsifying its times).

A window that already ended is a `409 {"error": "mute is not active"}`. Either
way a `MUTE_REVOKED` audit row is written — the audit trail is what is never
lost.""",
    parameters=[
        OpenApiParameter(
            "mute_id", int, OpenApiParameter.PATH, description="Mute window id (caller's org)."
        )
    ],
    request=None,
    responses={
        200: MuteRevokeResponseSerializer,
        **error_responses(401, 403, 404),
        409: CONFLICT_RESPONSE,
    },
)
@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def maintenance_mute_revoke(request, mute_id: int):
    org_id = _org_id(request)
    if org_id is None or not (_roles(request) & MANAGER_ROLES):
        return _forbidden("manager role required")

    row = (
        MaintenanceMute.objects.select_related("saloon")
        .filter(org_id=org_id, pk=mute_id)
        .first()
    )
    if row is None:
        return _not_found("mute not found")
    if row.saloon_id and _scoped_out(request, row.saloon):
        return _forbidden()

    now = timezone.now()
    if row.ends_at <= now:
        return _conflict("mute is not active")

    saloon, farm = row.saloon, row.saloon.farm if row.saloon_id else None
    audit = {
        "mute_id": row.pk,
        "starts_at": row.starts_at.isoformat(),
        "ends_at_before": row.ends_at.isoformat(),
    }

    with transaction.atomic():
        if row.starts_at > now:
            # Not started: nothing to shorten, nothing to keep.
            audit["mode"] = "deleted"
            AccountActivityLog.objects.create(
                actor=request.user,
                org_id=org_id,
                action=ACTION_MUTE_REVOKED,
                farm=farm,
                saloon=saloon,
                ip_address=request.META.get("REMOTE_ADDR"),
                details=audit,
            )
            row.delete()
            return Response(
                {
                    "mute_id": mute_id,
                    "saloon_id": str(saloon.pk) if saloon else None,
                    "ends_at": now.isoformat(),
                    "status": "REVOKED",
                }
            )

        audit["mode"] = "ended"
        try:
            row.ends_at = now
            row.save(update_fields=["ends_at"])
        except IntegrityError:  # pragma: no cover - CHECK(ends_at > starts_at)
            return _conflict("mute is not active")
        AccountActivityLog.objects.create(
            actor=request.user,
            org_id=org_id,
            action=ACTION_MUTE_REVOKED,
            farm=farm,
            saloon=saloon,
            ip_address=request.META.get("REMOTE_ADDR"),
            details=audit,
        )
    return Response(
        {
            "mute_id": row.pk,
            "saloon_id": str(row.saloon_id),
            "ends_at": row.ends_at.isoformat(),
            "status": "REVOKED",
        }
    )
