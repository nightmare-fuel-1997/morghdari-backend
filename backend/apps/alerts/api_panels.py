"""Granular panel surfaces — v0.9 M9-08/B3 (D24-2).

GET /panels/me/ resolves the D24-2 chain for the caller (any member);
override writes are owner-only (the admin panel must stay ownable by exactly
one role). Reviewer read-only (D24-1) falls out naturally: reviewers GET
like everyone, and no write path admits them.

Separate module (not api_follow): panels are §4n scope, not the F-wave.
Mounted on the same urls_follow router.
"""

from django.db import IntegrityError, transaction
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.accounts.views import ORG_MEMBER_ROLES
from apps.alerts.models import PanelDefinition, RolePanelPermission, UserPanelOverride
from apps.alerts.serializers_follow import (
    PanelOverrideRequestSerializer,
    PanelOverrideResponseSerializer,
    PanelResolveResponseSerializer,
    RolePermRequestSerializer,
    RolePermResponseSerializer,
)
from apps.alerts.services.panels import resolve_panels
from apps.auditing.models import AccountActivityLog
from apps.common.schemas import TAG_ALERTS, error_responses
from apps.monitoring.ops_services import InvalidPagination, parse_pagination

OWNER_ROLES = {"owner"}


def _org_id(request):
    return _claims(request).get("org_id")


def _roles(request) -> set:
    return set(_claims(request).get("roles", []))


def _forbidden(msg: str = "forbidden"):
    return Response({"error": msg}, status=403)


def _bad_request(msg: str):
    return Response({"error": msg}, status=400)


def _not_found(msg: str = "not found"):
    return Response({"error": msg}, status=404)


def _owner_only(request) -> bool:
    return bool(_org_id(request)) and bool(_roles(request) & OWNER_ROLES)


@extend_schema(
    tags=[TAG_ALERTS],
    operation_id="resolve_my_panels",
    summary="Resolve my visible panels (D24-2 chain)",
    description="""User override → role rows (deny wins) → panel default;
owner_only panels open for owners only. Any member reads (reviewer included —
panels are read-only for them by construction).""",
    responses={200: PanelResolveResponseSerializer, **error_responses(401, 403)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def panels_me(request):
    """GET /panels/me/ — resolved panel list for the caller."""
    org_id = _org_id(request)
    if org_id is None:
        return _forbidden("org context required")
    return Response({"results": resolve_panels(org_id, request.user, _roles(request))})


@extend_schema(
    methods=["GET"],
    tags=[TAG_ALERTS],
    operation_id="list_role_panel_permissions",
    summary="List role panel gates",
    description="Any member reads.",
    responses={200: RolePermResponseSerializer, **error_responses(401, 403)},
)
@extend_schema(
    methods=["POST"],
    tags=[TAG_ALERTS],
    operation_id="set_role_panel_permission",
    summary="Set a role panel gate (owner-only)",
    description="Upserts the (panel, role) row. Writes PANEL_ROLE_SET.",
    request=RolePermRequestSerializer,
    responses={200: RolePermResponseSerializer, **error_responses(400, 401, 403)},
    examples=[OpenApiExample("Gate lux panel", request_only=True,
                             value={"panel_id": "telemetry", "role": "worker", "allowed": False})],
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def role_panel_permissions(request):
    """GET/POST /panels/role-permissions/ (writes owner-only)."""
    org_id = _org_id(request)
    if org_id is None:
        return _forbidden("org context required")
    if request.method == "POST":
        if not _owner_only(request):
            return _forbidden("owner role required")
        panel_id = str(request.data.get("panel_id") or "").strip()
        role = str(request.data.get("role") or "").strip()
        allowed = request.data.get("allowed")
        if not PanelDefinition.objects.filter(pk=panel_id).exists():
            return _bad_request("unknown panel_id")
        if role not in ORG_MEMBER_ROLES:
            return _bad_request("unknown role")
        if not isinstance(allowed, bool):
            return _bad_request("allowed must be a boolean")
        row, _ = RolePanelPermission.objects.update_or_create(
            panel_id=panel_id, role=role, defaults={"allowed": allowed}
        )
        AccountActivityLog.objects.create(
            actor=request.user, org_id=org_id, action="PANEL_ROLE_SET",
            ip_address=request.META.get("REMOTE_ADDR"),
            details={"panel_id": panel_id, "role": role, "allowed": allowed},
        )
        return Response({"id": row.pk, "panel_id": panel_id, "role": role, "allowed": allowed})
    rows = RolePanelPermission.objects.order_by("panel_id", "role")
    try:
        limit, offset = parse_pagination(request)
    except InvalidPagination:
        return _bad_request("invalid pagination")
    total = rows.count()
    return Response(
        {
            "results": [
                {"id": r.pk, "panel_id": r.panel_id, "role": r.role, "allowed": r.allowed}
                for r in rows[offset : offset + limit]
            ],
            "count": total,
        }
    )


@extend_schema(
    tags=[TAG_ALERTS],
    operation_id="delete_role_panel_permission",
    summary="Delete a role panel gate (owner-only)",
    description="Falls back to the panel default. Unknown id is 404.",
    parameters=[OpenApiParameter("perm_id", int, OpenApiParameter.PATH, description="Permission row pk.")],
    responses={200: RolePermResponseSerializer, **error_responses(401, 403, 404)},
)
@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def role_panel_permission_delete(request, perm_id: int):
    """DELETE /panels/role-permissions/{id}/ (owner-only)."""
    if not _owner_only(request):
        return _forbidden("owner role required")
    row = RolePanelPermission.objects.filter(pk=perm_id).first()
    if row is None:
        return _not_found("permission not found")
    out = {"id": row.pk, "panel_id": row.panel_id, "role": row.role, "allowed": row.allowed}
    row.delete()
    AccountActivityLog.objects.create(
        actor=request.user, org_id=_org_id(request), action="PANEL_ROLE_UNSET",
        ip_address=request.META.get("REMOTE_ADDR"), details=out,
    )
    return Response(out)


@extend_schema(
    methods=["GET"],
    tags=[TAG_ALERTS],
    operation_id="list_user_panel_overrides",
    summary="List user panel overrides",
    description="Any member reads (org-scoped).",
    responses={200: PanelOverrideResponseSerializer, **error_responses(401, 403)},
)
@extend_schema(
    methods=["POST"],
    tags=[TAG_ALERTS],
    operation_id="set_user_panel_override",
    summary="Set a user panel override (owner-only)",
    description="""Upserts the (panel, user) row; wins over every role row
except on owner_only panels (which stay owner-only — fail-closed). The user
must be an active member of the caller's org. Writes PANEL_OVERRIDE_SET.""",
    request=PanelOverrideRequestSerializer,
    responses={200: PanelOverrideResponseSerializer, **error_responses(400, 401, 403)},
    examples=[OpenApiExample("Open reports", request_only=True,
                             value={"panel_id": "reports", "user_id": 7, "allowed": True})],
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def user_panel_overrides(request):
    """GET/POST /panels/user-overrides/ (writes owner-only)."""
    from apps.accounts.models import OrgMember

    org_id = _org_id(request)
    if org_id is None:
        return _forbidden("org context required")
    if request.method == "POST":
        if not _owner_only(request):
            return _forbidden("owner role required")
        panel_id = str(request.data.get("panel_id") or "").strip()
        if not PanelDefinition.objects.filter(pk=panel_id).exists():
            return _bad_request("unknown panel_id")
        try:
            user_id = int(request.data.get("user_id"))
        except (TypeError, ValueError):
            return _bad_request("user_id required and must be an integer")
        allowed = request.data.get("allowed")
        if not isinstance(allowed, bool):
            return _bad_request("allowed must be a boolean")
        member = OrgMember.objects.filter(user_id=user_id, org_id=org_id, is_active=True).first()
        if member is None:
            return _not_found("user is not an active member of this org")
        try:
            with transaction.atomic():
                row, _ = UserPanelOverride.objects.update_or_create(
                    panel_id=panel_id, user_id=user_id,
                    defaults={"org_id": org_id, "allowed": allowed},
                )
        except IntegrityError:
            return _bad_request("override violates panel constraints")
        AccountActivityLog.objects.create(
            actor=request.user, org_id=org_id, action="PANEL_OVERRIDE_SET",
            ip_address=request.META.get("REMOTE_ADDR"),
            details={"panel_id": panel_id, "user_id": user_id, "allowed": allowed},
        )
        return Response({"id": row.pk, "panel_id": panel_id, "user_id": user_id, "allowed": allowed},
                        status=status.HTTP_200_OK)
    rows = UserPanelOverride.objects.filter(org_id=org_id).order_by("panel_id", "user_id")
    try:
        limit, offset = parse_pagination(request)
    except InvalidPagination:
        return _bad_request("invalid pagination")
    total = rows.count()
    return Response(
        {
            "results": [
                {"id": r.pk, "panel_id": r.panel_id, "user_id": r.user_id, "allowed": r.allowed}
                for r in rows[offset : offset + limit]
            ],
            "count": total,
        }
    )


@extend_schema(
    tags=[TAG_ALERTS],
    operation_id="delete_user_panel_override",
    summary="Delete a user panel override (owner-only)",
    description="Unknown/other-org id is 404.",
    parameters=[OpenApiParameter("override_id", int, OpenApiParameter.PATH, description="Override row pk.")],
    responses={200: PanelOverrideResponseSerializer, **error_responses(401, 403, 404)},
)
@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def user_panel_override_delete(request, override_id: int):
    """DELETE /panels/user-overrides/{id}/ (owner-only)."""
    org_id = _org_id(request)
    if not _owner_only(request):
        return _forbidden("owner role required")
    row = UserPanelOverride.objects.filter(pk=override_id, org_id=org_id).first()
    if row is None:
        return _not_found("override not found")
    out = {"id": row.pk, "panel_id": row.panel_id, "user_id": row.user_id, "allowed": row.allowed}
    row.delete()
    AccountActivityLog.objects.create(
        actor=request.user, org_id=org_id, action="PANEL_OVERRIDE_UNSET",
        ip_address=request.META.get("REMOTE_ADDR"), details=out,
    )
    return Response(out)
