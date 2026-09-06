"""Ops REST API — M6-10 (contract v1.7.0, endpoints O1–O12; spec: docs/api-ops-v1.md).

All BearerAuth (default), all tenant-scoped via the JWT org_id claim + RLS;
role fences are explicit comparisons against the validated roles claim —
RLS stays the backstop, the comparison is the portable check (the M6-06
lesson: superuser/test connections bypass RLS).

Endpoints (verbatim contract paths):
  O1  GET  /api/v1/audit-logs/                     owner, manager
  O2  GET  /api/v1/thresholds/                     engineer+
  O3  PUT  /api/v1/thresholds/{id}/                manager+
  O4  GET  /api/v1/threshold-templates/            engineer+
  O5  GET  /api/v1/saloons/{id}/photos/            any member of owning org
  O6  POST /api/v1/saloons/{id}/photos/            engineer+
  O7  GET  /api/v1/gateways/                       manager+
  O8  GET  /api/v1/gateways/{slot}/                manager+
  O9  GET  /api/v1/gateways/{slot}/diagnostics/    engineer+
  O10 POST /api/v1/auth/password/change/           any authenticated user
  O11 GET  /api/v1/auth/sessions/                  any authenticated user (own)
  O12 DELETE /api/v1/auth/sessions/{id}/           owner of the session

فارسی: موجِ عملیاتی — خواندنِ ممیزها، آستانه‌ها، عکس‌ها، گیت‌وی‌ها و نشست‌ها؛
همه فقط افزوده، بدونِ تغییرِ هیچ مسیرِ قبلی.
"""

import uuid

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction as db_transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import UserSession
from apps.accounts.permissions import _claims
from apps.accounts.services import refresh_is_revoked, touch_session
from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule
from apps.auditing.models import AccountActivityLog
from apps.devices.models import (
    GatewayHardwareRegistry,
    GatewaySaloonZone,
    GatewaySlot,
    SaloonPhoto,
)
from apps.farms.models import Saloon
from apps.monitoring import ops_serializers as ser
from apps.monitoring import ops_services as svc
from apps.monitoring.ops_throttles import UserRateThrottle3PerMinute
from apps.telemetry.models import FlockCycle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken


def _org_claim(request) -> str | None:
    """The validated org_id claim; None ⇒ not a tenant caller (403)."""
    return _claims(request).get("org_id")


def _forbidden():
    return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)


def _not_found(msg="not found"):
    return Response({"error": msg}, status=status.HTTP_404_NOT_FOUND)


def _bad_request(msg):
    return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)


def _member_for(request, org_claim):
    """The caller's ACTIVE OrgMember row in the claim's org (role source)."""
    from apps.accounts.models import OrgMember

    return (
        OrgMember.objects.filter(user=request.user, org_id=org_claim, is_active=True)
        .order_by("joined_at", "id")
        .first()
    )


def _roles(request) -> set:
    return set(_claims(request).get("roles", []))


def _iso(dt) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


# ---------------------------------------------------------------------------
# O1 — GET /audit-logs/ (owner, manager)
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def audit_logs(request):
    org_claim = _org_claim(request)
    if not org_claim or not _roles(request) & {"owner", "manager"}:
        return _forbidden()
    try:
        limit, offset = svc.parse_pagination(request)
    except svc.InvalidPagination:
        return _bad_request("invalid pagination")
    qs = AccountActivityLog.objects.filter(org_id=org_claim).order_by("-timestamp", "-id")
    try:
        qs = svc.build_audit_filters(qs, request, org_claim)
    except svc.InvalidPagination:
        return _bad_request("invalid pagination")
    return Response(
        {
            "results": [ser.audit_item(r) for r in qs[offset : offset + limit]],
            "count": qs.count(),
        }
    )


# ---------------------------------------------------------------------------
# O2/O3 — /thresholds/ (engineer+ read, manager+ write)
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def thresholds(request):
    if not _roles(request) & {"owner", "manager", "engineer", "doctor"}:
        return _forbidden()
    try:
        limit, offset = svc.parse_pagination(request)
    except svc.InvalidPagination:
        return _bad_request("invalid pagination")
    # Org scope (spec §4: never cross-org data): rules of the caller's org's
    # templates PLUS the global catalog (template.org NULL = built-in). A
    # foreign org's template rules are invisible here and 404 on O3.
    from django.db.models import Q

    org_claim = _org_claim(request)  # scoped below; missing claim ⇒ 403 earlier
    qs = LifecycleThresholdRule.objects.filter(
        Q(template__org_id=org_claim) | Q(template__org_id__isnull=True)
    ).order_by("id")
    metric_key = request.query_params.get("metric_key")
    if metric_key:
        qs = qs.filter(metric=metric_key)
    saloon_id = request.query_params.get("saloon_id")
    if saloon_id:
        try:
            int(saloon_id)
        except (TypeError, ValueError):
            return _bad_request("invalid pagination")
        # Rules are template-scoped; the saloon param narrows to the rules of
        # the templates matching the saloon's ACTIVE flock's breed_type
        # (FlockCycle carries breed_type, not a template FK — the breed
        # column IS the linkage, same one O4 filters on).
        cycle = FlockCycle.objects.filter(saloon_id=saloon_id, end_date__isnull=True).first()
        if cycle is None:
            return Response({"results": [], "count": 0})
        template_ids = LifecycleTemplate.objects.filter(breed_type=cycle.breed_type).values_list(
            "template_id", flat=True
        )
        qs = qs.filter(template_id__in=template_ids)
    return Response(
        {
            "results": [ser.threshold_item(r) for r in qs[offset : offset + limit]],
            "count": qs.count(),
        }
    )


@api_view(["PUT"])
@permission_classes([IsAuthenticated])
def threshold_update(request, rule_id: int):
    if not _roles(request) & {"owner", "manager"}:
        return _forbidden()
    from django.db.models import Q

    # Write scope is OWN-org templates ONLY: global (template.org NULL) rules
    # stay readable via O2 but are never writable here — a shared catalog row
    # must not be editable across tenants (cross-org write hazard).
    rule = LifecycleThresholdRule.objects.filter(
        Q(id=rule_id),
        Q(template__org_id=str(_org_claim(request))),
    ).first()
    if rule is None:
        return _not_found("threshold not found")
    payload = request.data if isinstance(request.data, dict) else {}
    # Accept the contract's {min, max} keys (aliases: the column names) and
    # validate the NORMALIZED body so both key styles are guarded.
    body = {
        "min_value": payload.get("min", payload.get("min_value")),
        "max_value": payload.get("max", payload.get("max_value")),
    }
    error = svc.threshold_range_error(rule.metric, body)
    if error:
        return _bad_request(error)
    updates = []
    for field in ("min_value", "max_value"):
        if body[field] is not None:
            setattr(rule, field, body[field])
            updates.append(field)
    with db_transaction.atomic():
        rule.save(update_fields=updates)
        # Contract O3: the edit writes an audit row (provisioning_audit_log-
        # style); org scope keeps it visible to O1 and out of foreign orgs.
        AccountActivityLog.objects.create(
            actor=request.user,
            org_id=rule.template.org_id if (rule.template_id and rule.template.org_id) else _org_claim(request),
            action="THRESHOLD_UPDATED",
            details={
                "rule_id": rule.id,
                "metric": rule.metric,
                "min_value": float(rule.min_value) if rule.min_value is not None else None,
                "max_value": float(rule.max_value) if rule.max_value is not None else None,
                "day_from": rule.day_from,
                "day_to": rule.day_to,
            },
        )
    return Response(ser.threshold_item(rule))


# ---------------------------------------------------------------------------
# O4 — GET /threshold-templates/ (engineer+)
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def threshold_templates(request):
    if not _roles(request) & {"owner", "manager", "engineer", "doctor"}:
        return _forbidden()
    try:
        limit, offset = svc.parse_pagination(request)
    except svc.InvalidPagination:
        return _bad_request("invalid pagination")
    qs = LifecycleTemplate.objects.all().order_by("template_id")
    breed = request.query_params.get("breed_type")
    if breed:
        qs = qs.filter(breed_type=breed)
    return Response(
        {
            "results": [ser.template_item(t) for t in qs[offset : offset + limit]],
            "count": qs.count(),
        }
    )


# ---------------------------------------------------------------------------
# O5/O6 — /saloons/{id}/photos/ (read: any member; write: engineer+)
# ---------------------------------------------------------------------------


def _saloon_or_none(request, saloon_id: int):
    """Resolve the saloon INSIDE the claim's tenant context — RLS makes a
    foreign saloon invisible; the explicit org comparison (string-normalized:
    the claim rides the JWT as str, the column is a UUID) is the portable
    check (M6-06 lesson) and the fallthrough is the contractual 404."""
    from apps.tenants.context import tenant_context

    org_claim = _org_claim(request)
    if not org_claim:
        return None
    with db_transaction.atomic():
        with tenant_context(org_claim):
            saloon_org = (
                Saloon.objects.filter(pk=saloon_id).values_list("org_id", flat=True).first()
            )
    return saloon_org if saloon_org and str(saloon_org) == str(org_claim) else None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def saloon_photos(request, saloon_id: int):
    """O5 (GET — any member of the owning org) + O6 (POST — engineer+) on the
    ONE contractual path; methods dispatch to the private halves below."""
    if request.method == "GET":
        return _photos_list(request, saloon_id)
    return _photo_upload(request, saloon_id)


def _photos_list(request, saloon_id: int):
    if not _org_claim(request):
        return _forbidden()
    if _saloon_or_none(request, saloon_id) is None:
        return _not_found("saloon not found")
    try:
        limit, offset = svc.parse_pagination(request)
    except svc.InvalidPagination:
        return _bad_request("invalid pagination")
    qs = SaloonPhoto.objects.filter(saloon_id=saloon_id).order_by("-uploaded_at", "-id")
    return Response(
        {
            "results": [ser.photo_item(p) for p in qs[offset : offset + limit]],
            "count": qs.count(),
        }
    )


def _photo_upload(request, saloon_id: int):
    if not _roles(request) & {"owner", "manager", "engineer"}:
        return _forbidden()
    if _saloon_or_none(request, saloon_id) is None:
        return _not_found("saloon not found")
    error = svc.photo_error(request.FILES.get("file"))
    if error:
        return _bad_request(error)
    uploaded = request.FILES["file"]
    taken_at = svc.parse_iso_z(request.data.get("taken_at") or "")
    # Storage backend (S3/local) is a deploy setting, not part of the contract
    # (spec §2 O6); v0.6 stores the object in MEDIA_ROOT and persists a
    # serviceable URL. Subdir keeps the photos folder enumerable per saloon.
    import os

    from django.core.files.storage import default_storage

    ext = svc.ALLOWED_PHOTO_TYPES.get(uploaded.content_type, ".jpg")
    name = default_storage.save(
        f"saloon-photos/{saloon_id}/{uuid.uuid4().hex}{ext}", uploaded
    )
    photo = SaloonPhoto.objects.create(
        saloon_id=saloon_id,
        org_id=_org_claim(request),
        uploaded_by=request.user,
        file_url=default_storage.url(name),
        description=(request.data.get("caption") or "")[:255] or None,
        uploaded_at=taken_at or timezone.now(),
    )
    return Response(ser.photo_item(photo), status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# O7/O8/O9 — /gateways/ (manager+ reads; diagnostics: engineer+)
# ---------------------------------------------------------------------------


def _gateways_qs(request):
    """Org-scoped GatewaySlot queryset with O7's optional filters."""
    qs = GatewaySlot.objects.filter(org_id=_org_claim(request)).order_by("slot_id")
    farm_id = request.query_params.get("farm_id")
    if farm_id:
        try:  # Farm PK is an INT AutoField — validate as int, never UUID
            int(farm_id)
        except (TypeError, ValueError):
            raise svc.InvalidPagination()
        qs = qs.filter(farm_id=int(farm_id))
    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(provisioning_status=status_filter)
    return qs


def _saloon_counts(slot_ids) -> dict:
    return {
        row["gateway_slot_id"]: row["n"]
        for row in GatewaySaloonZone.objects.filter(gateway_slot_id__in=list(slot_ids))
        .values("gateway_slot_id")
        .annotate(n=Count("saloon"))
    }


def _last_seen_map(slot_ids) -> dict:
    """slot → diag-reported last_seen ISO, read from the diag register.
    Redis down ⇒ every value None (honest missing, never fabricated)."""
    out = {}
    for slot_id in slot_ids:
        diag = svc.read_last_diagnostics(slot_id)
        out[slot_id] = _iso_ts(diag["reported_at"]) if diag else None
    return out


def _iso_ts(raw):
    """diag stores the gateway's device_ts VERBATIM — a string already in
    contract shape; return it as-is (None stays None)."""
    return raw


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def gateways(request):
    if not _roles(request) & {"owner", "manager"}:
        return _forbidden()
    try:
        limit, offset = svc.parse_pagination(request)
    except svc.InvalidPagination:
        return _bad_request("invalid pagination")
    try:
        qs = _gateways_qs(request)
    except svc.InvalidPagination:
        return _bad_request("invalid pagination")
    slots = list(qs[offset : offset + limit])
    counts = _saloon_counts([s.slot_id for s in slots])
    last_seen = _last_seen_map([s.slot_id for s in slots])
    return Response(
        {
            "results": [
                ser.gateway_item(s, counts.get(s.slot_id, 0), last_seen.get(s.slot_id))
                for s in slots
            ],
            "count": qs.count(),
        }
    )


def _gateway_or_none(request, slot: str):
    """Org-comparison resolution (RLS backstop; explicit check is portable —
    string-normalized on both sides: JWT claim is str, column is a UUID)."""
    return GatewaySlot.objects.filter(
        slot_id=slot, org_id=str(_org_claim(request))
    ).first()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def gateway_detail(request, slot: str):
    if not _roles(request) & {"owner", "manager"}:
        return _forbidden()
    slot_row = _gateway_or_none(request, slot)
    if slot_row is None:
        return _not_found("gateway not found")
    zones = GatewaySaloonZone.objects.filter(gateway_slot=slot_row).select_related("saloon")
    # related_name="+" suppresses the reverse accessor — query the registry
    # directly for the active (never-removed) hardware line.
    active_hw = GatewayHardwareRegistry.objects.filter(slot=slot_row, removed_at__isnull=True).first()
    diag = svc.read_last_diagnostics(slot)
    return Response(
        {
            "slot_id": slot_row.slot_id,
            "farm_id": slot_row.farm_id,
            "provisioning_status": slot_row.provisioning_status,
            "last_seen_at": _iso_ts(diag["reported_at"]) if diag else None,
            "saloon_count": zones.count(),
            "saloon_zones": [
                {
                    "saloon_id": z.saloon_id,
                    "saloon_name": z.saloon.name if z.saloon else None,
                    "assigned_at": _iso(z.assigned_at),
                }
                for z in zones
            ],
            "active_hardware_id": active_hw.hardware_id if active_hw else None,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def gateway_diagnostics(request, slot: str):
    if not _roles(request) & {"owner", "manager", "engineer", "doctor"}:
        return _forbidden()
    slot_row = _gateway_or_none(request, slot)
    if slot_row is None:
        return _not_found("gateway not found")
    diag = svc.read_last_diagnostics(slot)
    if diag is None:
        return _not_found("no diagnostics yet")
    return Response(diag)


# ---------------------------------------------------------------------------
# O10 — POST /auth/password/change/ (auth_strict: 3/min per user)
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle3PerMinute])
def password_change(request):
    payload = request.data if isinstance(request.data, dict) else {}
    current = payload.get("current_password") or ""
    new = payload.get("new_password") or ""

    if not request.user.check_password(current):
        return Response({"error": "wrong current password"}, status=status.HTTP_401_UNAUTHORIZED)
    if new == current:
        return _bad_request("new password must differ from current")
    if len(new) < svc.MIN_PASSWORD_LENGTH:
        return _bad_request("new password too short (min 10)")
    if new.lower() in svc.COMMON_PASSWORDS:
        return _bad_request("password is too common")
    try:
        validate_password(new, user=request.user)
    except DjangoValidationError:
        return _bad_request("password rejected")

    request.user.set_password(new)
    request.user.save(update_fields=["password"])
    # House posture: rotate the register — the OLD refresh lines die (their
    # jti rows are revoked), the new login creates fresh rows.
    UserSession.objects.filter(user=request.user, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    return Response({"status": "ok"})


# ---------------------------------------------------------------------------
# O11/O12 — /auth/sessions/ (own sessions only)
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sessions(request):
    rows = list(
        UserSession.objects.filter(user=request.user, revoked_at__isnull=True).order_by("-last_seen_at", "-id")
    )
    sid = _claims(request).get("sid")
    return Response(
        [
            {
                "id": row.id,
                "created_at": _iso(row.created_at),
                "last_seen_at": _iso(row.last_seen_at),
                "ip": row.ip,
                "user_agent": row.user_agent,
                "current": bool(sid and str(row.refresh_jti) == str(sid)),
            }
            for row in rows
        ]
    )


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def session_revoke(request, session_id: int):
    """Foreign/missing ids answer IDENTICAL 404s — never an existence oracle
    (spec §4). Revocation kills the refresh line: RefreshView refuses the
    revoked jti on its next use (access token inside its 15-min expiry)."""
    row = UserSession.objects.filter(id=session_id, user=request.user).first()
    if row is None:
        return _not_found("session not found")
    row.revoked_at = timezone.now()
    row.save(update_fields=["revoked_at"])
    return Response({"status": "revoked", "id": row.id})
