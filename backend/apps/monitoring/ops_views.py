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

v1.7.1 (2026-09-12) — the template WRITE surface, additive to the wave above:
  O13 POST   /api/v1/threshold-templates/                 engineer+ (farm-scoped)
  O14 PUT    /api/v1/threshold-templates/{id}/            engineer+ (farm-scoped)
  O15 POST   /api/v1/threshold-templates/{id}/publish/    owner, manager
  O16 GET    /api/v1/saloons/{id}/threshold-template/      any member of the org
      PUT    /api/v1/saloons/{id}/threshold-template/      owner, manager

فارسی: موجِ عملیاتی — خواندنِ ممیزها، آستانه‌ها، عکس‌ها، گیت‌وی‌ها و نشست‌ها؛
همه فقط افزوده، بدونِ تغییرِ هیچ مسیرِ قبلی.
"""

import uuid

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.db.models import Count
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import UserSession
from apps.accounts.permissions import _claims, scoped_farms
from apps.accounts.services import refresh_is_revoked, touch_session
from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule
from apps.auditing.models import AccountActivityLog
from apps.devices.models import (
    GatewayHardwareRegistry,
    GatewaySaloonZone,
    GatewaySlot,
    SaloonPhoto,
)
from apps.common.schemas import RATE_LIMITED_RESPONSE, TAG_OPS, error_responses
from apps.alerts.services.thresholds import effective_template
from apps.farms.models import Farm, Saloon
from apps.monitoring import ops_serializers as ser
from apps.monitoring import ops_services as svc
from apps.monitoring.ops_throttles import UserRateThrottle3PerMinute
from apps.monitoring.ops_serializers import (
    AuditLogListResponseSerializer,
    SaloonTemplateAssignRequestSerializer,
    SaloonTemplateResponseSerializer,
    ThresholdTemplateCreateRequestSerializer,
    ThresholdTemplateDetailSerializer,
    ThresholdTemplatePublishRequestSerializer,
    ThresholdTemplateUpdateRequestSerializer,
    GatewayDetailResponseSerializer,
    GatewayDiagnosticsSerializer,
    GatewayListResponseSerializer,
    PasswordChangeRequestSerializer,
    PasswordChangeResponseSerializer,
    PhotoItemSerializer,
    PhotoListResponseSerializer,
    PhotoUploadRequestSerializer,
    SessionItemSerializer,
    SessionRevokeResponseSerializer,
    ThresholdItemSerializer,
    ThresholdListResponseSerializer,
    ThresholdTemplateListResponseSerializer,
    ThresholdUpdateRequestSerializer,
)
from apps.telemetry.models import FlockCycle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

# O1/O2/O4/O5/O7 share the spec §1 pagination contract (limit ≤ 500, offset ≥ 0).
_PAGING_PARAMS = [
    OpenApiParameter(
        "limit", int, OpenApiParameter.QUERY, required=False, description="Page size (default 50, max 500)."
    ),
    OpenApiParameter(
        "offset", int, OpenApiParameter.QUERY, required=False, description="Page start (default 0)."
    ),
]


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


@extend_schema(
    tags=[TAG_OPS],
    operation_id="list_audit_logs",
    summary="O1 — org audit log (owner, manager)",
    description="""Newest-first `{results, count}` page of the org's activity log
(`created_at` ← `AccountActivityLog.timestamp`). Row objects are contract
shapes; `details` is the free-form action payload.

Filters: `actor_id`, `action`, `farm_id`, `saloon_id`, `from`/`to` (ISO Z).
A malformed scalar — including a non-numeric id or an unparseable timestamp
— is a 400 `invalid pagination`, never a silent full-table answer.""",
    parameters=[
        *_PAGING_PARAMS,
        OpenApiParameter("actor_id", int, OpenApiParameter.QUERY, required=False, description="Filter to one acting user."),
        OpenApiParameter("action", str, OpenApiParameter.QUERY, required=False, description="Exact action code, e.g. THRESHOLD_UPDATED."),
        OpenApiParameter("farm_id", int, OpenApiParameter.QUERY, required=False, description="Filter to one farm (int PK)."),
        OpenApiParameter("saloon_id", int, OpenApiParameter.QUERY, required=False, description="Filter to one saloon (int PK)."),
        OpenApiParameter("from", str, OpenApiParameter.QUERY, required=False, description="ISO Z timestamp window start (timestamp ≥)."),
        OpenApiParameter("to", str, OpenApiParameter.QUERY, required=False, description="ISO Z timestamp window end (timestamp ≤)."),
    ],
    request=None,
    responses={
        200: AuditLogListResponseSerializer,
        **error_responses(400, 401, 403),
    },
    examples=[
        OpenApiExample(
            "Audit page",
            response_only=True,
            value={
                "results": [
                    {
                        "id": "812",
                        "actor_id": 42,
                        "username": "engineer_a",
                        "action": "THRESHOLD_UPDATED",
                        "details": {"rule_id": 7, "metric": "temp", "min_value": 20.0, "max_value": 26.5},
                        "created_at": "2026-08-18T09:14:02Z",
                    }
                ],
                "count": 1,
            },
        )
    ],
)
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


@extend_schema(
    tags=[TAG_OPS],
    operation_id="list_thresholds",
    summary="O2 — threshold rules (engineer+)",
    description="""`{results, count}` page of the caller's org template rules **plus** the
global built-in catalog (`template.org IS NULL`); a foreign org's template
rules are invisible here and 404 on O3.

The rule's `min`/`max` are the contract keys; `day_from`/`day_to` bound the
flock-age window. Filter by `metric_key`; `saloon_id` narrows to the rules of
the templates matching that saloon's ACTIVE flock breed — a saloon with no
active flock is an **empty 200** `{"results": [], "count": 0}`.""",
    parameters=[
        *_PAGING_PARAMS,
        OpenApiParameter(
            "metric_key",
            str,
            OpenApiParameter.QUERY,
            required=False,
            description="Filter to one metric (source column: metric).",
        ),
        OpenApiParameter(
            "saloon_id",
            int,
            OpenApiParameter.QUERY,
            required=False,
            description="Narrow to the templates matching that saloon's active flock breed.",
        ),
    ],
    request=None,
    responses={
        200: ThresholdListResponseSerializer,
        **error_responses(400, 401, 403),
    },
)
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


@extend_schema(
    tags=[TAG_OPS],
    operation_id="update_threshold",
    summary="O3 — edit a threshold rule (manager+)",
    description="""Partial PUT: send `min` and/or `max` (column-name aliases
`min_value`/`max_value` are accepted too). Rules are **template**-scoped, and
only the caller's OWN-org templates are writable — global built-in rules stay
readable via O2 but always 404 here (a shared catalog row must never be
editable across tenants).

Bounds are metric-keyed (`THRESHOLD_BOUNDS`), the `min < max` rule always
applies, and an out-of-bounds or non-numeric value is a 400. Every accepted
edit writes a `THRESHOLD_UPDATED` audit row (visible in O1) in the same
transaction as the update. Returns the updated rule item.""",
    parameters=[
        OpenApiParameter(
            "rule_id", int, OpenApiParameter.PATH, description="Threshold rule id."
        )
    ],
    request=ThresholdUpdateRequestSerializer,
    responses={
        200: ThresholdItemSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Tighten the temp band",
            request_only=True,
            value={"min": 20.0, "max": 26.5},
        )
    ],
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


@extend_schema(
    methods=["GET"],
    tags=[TAG_OPS],
    operation_id="list_threshold_templates",
    summary="O4 — lifecycle threshold templates (engineer+)",
    description="""The caller's catalog of `lifecycle_templates` — the templates whose rules O2
returns. Scoped to the caller's OWN org PLUS the org-NULL built-in rows: a
foreign org's template is invisible here (and 404s on O13–O15), never a
cross-tenant leak.

`breed_type` filters the catalog and `status` filters `DRAFT` vs `PUBLISHED`
(v1.7.1) — **only a PUBLISHED template arms alarms**, so a client that wants
"what is live" must say `status=PUBLISHED`. `is_default` marks the org's
default row and the built-in one.

Read-only *for templates* even after v1.7.1: authoring a draft is O13, and
editing/publishing/assigning are O14–O16.""",
    parameters=[
        *_PAGING_PARAMS,
        OpenApiParameter(
            "breed_type",
            str,
            OpenApiParameter.QUERY,
            required=False,
            description="Filter to one breed type.",
        ),
        OpenApiParameter(
            "status",
            str,
            OpenApiParameter.QUERY,
            required=False,
            description="DRAFT | PUBLISHED (only PUBLISHED arms alarms).",
        ),
    ],
    request=None,
    responses={
        200: ThresholdTemplateListResponseSerializer,
        **error_responses(400, 401, 403),
    },
)
@extend_schema(
    methods=["POST"],
    tags=[TAG_OPS],
    operation_id="create_threshold_template",
    summary="O13 — author a threshold-template draft (engineer+, farm-scoped)",
    description="""Creates a **DRAFT** template with its day-ranged band list. A draft never
arms a saloon — resolution considers PUBLISHED templates only (O15) — so an
engineer can author one without touching a live band.

**Scope (fail-closed).** owner/manager may author anywhere in their org and
may omit `farm_id` (an org-wide draft). An **engineer** must stay inside the
farms granted via `account_role_scopes`; if exactly one farm is in scope
`farm_id` may be omitted, and if several are in scope it is required. An
engineer with **no** scope row has no template write at all → 403.

Bands are validated: known metric, integer day window in [0, 400], numeric
min/max inside the O3 sanity bounds, `min < max`, an `nh3` band must carry a
max (NH3 can never be silenced), and two bands of the same metric may not
overlap (overlap would make "which max applies" a row-order accident).

Writes a `THRESHOLD_TEMPLATE_CREATED` audit row in the same transaction.""",
    request=ThresholdTemplateCreateRequestSerializer,
    responses={
        201: ThresholdTemplateDetailSerializer,
        **error_responses(400, 401, 403, 404),
    },
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def threshold_templates(request):
    """O4 (GET — catalog) + O13 (POST — author a draft) on the ONE contractual
    path, the O5/O6 pattern: a second `path()` for the same route would shadow
    one of the methods and answer 405."""
    if request.method == "POST":
        return _threshold_template_create(request)
    return _threshold_templates_list(request)


def _threshold_templates_list(request):
    if not _roles(request) & {"owner", "manager", "engineer", "doctor"}:
        return _forbidden()
    try:
        limit, offset = svc.parse_pagination(request)
    except svc.InvalidPagination:
        return _bad_request("invalid pagination")
    # Org scope (spec §4: never cross-org data): own-org templates + the
    # org-NULL built-in catalog. Before v1.7.1 this read `objects.all()`, which
    # only looked correct because RLS filters it on a real (non-superuser)
    # connection — on a dev/test superuser it listed FOREIGN orgs' templates.
    from django.db.models import Q

    qs = (
        LifecycleTemplate.objects.filter(
            Q(org_id=str(_org_claim(request))) | Q(org__isnull=True)
        ).order_by("template_id")
    )
    breed = request.query_params.get("breed_type")
    if breed:
        qs = qs.filter(breed_type=breed)
    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)
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


@extend_schema(
    methods=["GET"],
    tags=[TAG_OPS],
    operation_id="list_saloon_photos",
    summary="O5 — saloon photo gallery (any member)",
    description="""`{results, count}` page of the saloon's photos, newest first.
`url` is a serviceable file URL (`file_url`), `taken_at` ← `uploaded_at`
(ISO Z), and `uploaded_by` is the recording user.

The saloon is resolved **inside** the claim's tenant context, so a foreign or
unknown id is an identical 404 — never an existence oracle.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        ),
        *_PAGING_PARAMS,
    ],
    request=None,
    responses={
        200: PhotoListResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
)
@extend_schema(
    methods=["POST"],
    tags=[TAG_OPS],
    operation_id="upload_saloon_photo",
    summary="O6 — upload a saloon photo (engineer+)",
    description="""Multipart upload: `file` is required (jpg/png, ≤10 MB — content type and
size are validated server-side), optional `caption` (stored as `description`,
≤255 chars) and optional `taken_at` (ISO Z; defaults to now when omitted or
unparseable — an invalid value is NOT a 400 on this field).

Storage backend is a deploy setting (v0.6 = MEDIA_ROOT); the response returns
a serviceable URL, never a storage path. Foreign/unknown saloon → 404.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        )
    ],
    request=PhotoUploadRequestSerializer,
    responses={
        201: PhotoItemSerializer,
        **error_responses(400, 401, 403, 404),
    },
)
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


@extend_schema(
    tags=[TAG_OPS],
    operation_id="list_gateways",
    summary="O7 — org gateways (manager+)",
    description="""`{results, count}` page of the org's gateway slots with their saloon-zone
count. `last_seen_at` comes from the retained diagnostics register (O9's
store) — Redis down means `null`, an honest missing value, never a fabricated
timestamp.

`farm_id` (int PK) and `status` filter the list; a malformed `farm_id` is a
400 `invalid pagination`.""",
    parameters=[
        *_PAGING_PARAMS,
        OpenApiParameter("farm_id", int, OpenApiParameter.QUERY, required=False, description="Filter to one farm (int PK)."),
        OpenApiParameter("status", str, OpenApiParameter.QUERY, required=False, description="Filter to one provisioning status."),
    ],
    request=None,
    responses={
        200: GatewayListResponseSerializer,
        **error_responses(400, 401, 403),
    },
)
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


@extend_schema(
    tags=[TAG_OPS],
    operation_id="get_gateway_detail",
    summary="O8 — gateway detail (manager+)",
    description="""Gateway row plus its saloon zones and the active hardware line
(never-removed registry entry). `saloon_zones[]` carries `saloon_id`,
`saloon_name` and `assigned_at`; `active_hardware_id` is null when no line is
registered. Unknown/foreign slot → 404.""",
    parameters=[
        OpenApiParameter(
            "slot", str, OpenApiParameter.PATH, description="Gateway slot id."
        )
    ],
    request=None,
    responses={
        200: GatewayDetailResponseSerializer,
        **error_responses(401, 403, 404),
    },
)
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


@extend_schema(
    tags=[TAG_OPS],
    operation_id="get_gateway_diagnostics",
    summary="O9 — last reported diagnostics (engineer+)",
    description="""The retained `diag:last:{slot}` hash as the device reported it —
`reported_at` is the gateway's own `device_ts` string, verbatim, and every
field is honest-null when the device did not send it.

Unknown/foreign slot, or a gateway that has never reported, is the same 404
`{"error": "no diagnostics yet"}` / not-found envelope.""",
    parameters=[
        OpenApiParameter(
            "slot", str, OpenApiParameter.PATH, description="Gateway slot id."
        )
    ],
    request=None,
    responses={
        200: GatewayDiagnosticsSerializer,
        **error_responses(401, 403, 404),
    },
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


@extend_schema(
    tags=[TAG_OPS],
    operation_id="change_password",
    summary="O10 — change my password",
    description="""Requires the current password (`401 wrong current password` when it does
not match). The new password must differ from the current one, be ≥10 chars,
stay off the common-password denylist, and pass Django's configured validators
— each failure is a 400 with a specific message, never a generic rejection.

Success rotates the session register: every active refresh line is revoked
(the next refresh is refused) while the current access token lives out its
15 minutes. Throttled on the strict lane — 3/min per user.""",
    request=PasswordChangeRequestSerializer,
    responses={
        200: PasswordChangeResponseSerializer,
        **error_responses(400, 401),
        429: RATE_LIMITED_RESPONSE,
    },
    examples=[
        OpenApiExample(
            "Change password",
            request_only=True,
            value={"current_password": "old-secret-1", "new_password": "farm-floor-2026"},
        )
    ],
)
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


@extend_schema(
    tags=[TAG_OPS],
    operation_id="list_my_sessions",
    summary="O11 — my live sessions",
    description="""Bare list (no envelope) of the caller's non-revoked refresh lines,
newest-seen first. `current: true` marks the presenting session (the claim's
`sid` matches the row's `refresh_jti`).

Scope is always the caller: there is no parameter to read someone else's
sessions, and a revoked row disappears from this list.""",
    request=None,
    responses={200: SessionItemSerializer(many=True), **error_responses(401)},
)
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


@extend_schema(
    tags=[TAG_OPS],
    operation_id="revoke_my_session",
    summary="O12 — revoke one of my sessions",
    description="""Revokes the refresh line (the next refresh on that `jti` is refused; the
access token still lives out its 15 minutes).

The session must belong to the caller: a foreign **and** a missing id answer
an identical 404 — never an existence oracle (spec §4). Success is
`{"status": "revoked", "id": …}`.""",
    parameters=[
        OpenApiParameter(
            "session_id", int, OpenApiParameter.PATH, description="The caller's session id (O11)."
        )
    ],
    request=None,
    responses={
        200: SessionRevokeResponseSerializer,
        **error_responses(401, 404),
    },
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


# ---------------------------------------------------------------------------
# v1.7.1 — the threshold-template WRITE surface (O13–O16)
#
# Shape (owner decision 2026-09-12): an ENGINEER authors farm-scoped DRAFTS, a
# manager PUBLISHES one and ASSIGNS it to saloons. Two rules carry the security
# weight, and both are enforced HERE rather than in the model:
#
#   * FAIL CLOSED on scope. `scoped_farms()` returns None for a member with NO
#     scope row, and its own module docstring defines None as "the whole org" —
#     right for reads, catastrophic for this surface, where it would turn
#     "farm-scoped engineer" into "every farm in the org". This module inverts
#     None into the EMPTY set: an unscoped engineer has no template write at
#     all. (Before v1.7.1 nothing called scoped_farms at all, so the function
#     was tested-but-unmounted dead code — this is its first real caller.)
#   * ONLY PUBLISHED templates can be assigned or resolved, and only a manager
#     publishes. A draft therefore cannot reach a saloon, which is what makes
#     it safe for an engineer to author one unattended.
#
# Every accepted write lands an account_activity_logs row in the SAME
# transaction as the change it describes (the O3 precedent): "why did nobody
# get paged last Tuesday" is answerable from that table and nowhere else.
#
# فارسی: پیش‌نویس را مهندس و در دامنهٔ مزرعه‌های مجاز می‌سازد، انتشار و تخصیص
# با مدیر است؛ بی‌دامنه یعنی بی‌اجازه، و فقط قالبِ منتشرشده می‌تواند سالن را مسلح کند.
# ---------------------------------------------------------------------------

TEMPLATE_AUTHOR_ROLES = {"owner", "manager", "engineer"}
TEMPLATE_MANAGE_ROLES = {"owner", "manager"}

# account_activity_logs.action codes for this surface.
ACTION_TEMPLATE_CREATED = "THRESHOLD_TEMPLATE_CREATED"
ACTION_TEMPLATE_UPDATED = "THRESHOLD_TEMPLATE_UPDATED"
ACTION_TEMPLATE_PUBLISHED = "THRESHOLD_TEMPLATE_PUBLISHED"
ACTION_SALOON_ASSIGNED = "SALOON_TEMPLATE_ASSIGNED"
ACTION_SALOON_CLEARED = "SALOON_TEMPLATE_CLEARED"


def _conflict(msg):
    return Response({"error": msg}, status=status.HTTP_409_CONFLICT)


def _authoring_farms(request):
    """The farms this caller may AUTHOR a template on.

    ``None``  = org-level authority (owner/manager) — never narrowed.
    a set      = exactly the granted farm ids. An ENGINEER with no scope row
                 gets the EMPTY set, which is this module's fail-closed rule.
    """
    if _roles(request) & TEMPLATE_MANAGE_ROLES:
        return None
    scoped = scoped_farms(request.user, _org_claim(request))
    if scoped is None:
        return set()
    return set(scoped.values_list("pk", flat=True))


def _authoring_scope_error(request, template):
    """403 unless the caller may author/edit THIS template (None = allowed)."""
    limit_to = _authoring_farms(request)
    if limit_to is None:
        return None
    if not limit_to or template.farm_id is None or template.farm_id not in limit_to:
        # An org-wide draft is org-level business, and another farm's draft is
        # outside the grant — both are the same refusal (never an oracle).
        return _forbidden()
    return None


def _authoring_farm_id(request, raw_farm_id):
    """(farm_id, error_response) — resolves the draft's owning farm.

    Org-level callers may omit it (org-wide draft) or name any own-org farm.
    An engineer is always inside the grant, and a single-farm engineer does not
    have to restate it.
    """
    limit_to = _authoring_farms(request)
    if limit_to is None:
        if raw_farm_id is None:
            return None, None
        farm_id = _as_int(raw_farm_id)
        if farm_id is None:
            return None, _bad_request("invalid farm_id")
        if not Farm.objects.filter(pk=farm_id, org_id=str(_org_claim(request))).exists():
            return None, _not_found("farm not found")
        return farm_id, None
    if not limit_to:
        return None, _forbidden()  # unscoped engineer: no template write at all
    if raw_farm_id is None:
        if len(limit_to) > 1:
            return None, _bad_request("farm_id is required when several farms are in scope")
        return next(iter(limit_to)), None
    farm_id = _as_int(raw_farm_id)
    if farm_id is None:
        return None, _bad_request("invalid farm_id")
    if farm_id not in limit_to:
        return None, _forbidden()
    return farm_id, None


def _as_int(raw):
    """Strict int coercion (bool and 1.5 are both invalid, not coerced)."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _own_org_template(request, template_id: int):
    """Own-org template or None — the org-NULL built-in catalog and every other
    org's row are indistinguishable 404s here, exactly like O3's write scope."""
    return LifecycleTemplate.objects.filter(
        template_id=template_id, org_id=str(_org_claim(request))
    ).first()


def _rules_of(template):
    return list(
        LifecycleThresholdRule.objects.filter(template=template).order_by(
            "metric", "day_from", "id"
        )
    )


def _replace_rules(template, rules):
    """Replace a DRAFT's band list wholesale.

    The SET is the unit of meaning: overlap is validated across rules, so a
    partial edit could leave a combination no single request ever contained.
    `bulk_create` also (deliberately) skips the rules post_save signal — the
    draft cannot change any live hash, so there is nothing to re-cache.
    """
    LifecycleThresholdRule.objects.filter(template=template).delete()
    LifecycleThresholdRule.objects.bulk_create(
        [
            LifecycleThresholdRule(
                template=template,
                day_from=int(raw["day_from"]),
                day_to=int(raw["day_to"]),
                metric=raw.get("metric_key", raw.get("metric")),
                min_value=raw.get("min", raw.get("min_value")),
                max_value=raw.get("max", raw.get("max_value")),
            )
            for raw in rules
        ]
    )


def _template_audit(request, action, template, **extra):
    AccountActivityLog.objects.create(
        actor=request.user,
        org_id=_org_claim(request),
        farm_id=template.farm_id,
        action=action,
        details={
            "template_id": template.template_id,
            "name": template.name,
            "status": template.status,
            **extra,
        },
    )


def _saloon_row(request, saloon_id: int):
    """Own-org Saloon or None — foreign and unknown are identical (M6-06
    lesson: the explicit string-normalized comparison is the portable check,
    RLS stays the backstop)."""
    org_claim = _org_claim(request)
    if not org_claim:
        return None
    row = Saloon.objects.filter(pk=saloon_id).first()
    return row if row is not None and str(row.org_id) == str(org_claim) else None


def _saloon_flock(saloon):
    """The flock the saloon's hash is built from (open, else the latest)."""
    return (
        FlockCycle.objects.filter(saloon=saloon, end_date__isnull=True)
        .order_by("-start_date")
        .first()
        or FlockCycle.objects.filter(saloon=saloon).order_by("-start_date").first()
    )


def _threshold_template_create(request):
    """O13 half — reached through the dispatcher above (which owns the schema)."""
    if not _org_claim(request):
        return _forbidden()
    if not _roles(request) & TEMPLATE_AUTHOR_ROLES:
        return _forbidden()
    payload = request.data if isinstance(request.data, dict) else {}

    name = str(payload.get("name") or "").strip()
    if not name or len(name) > svc.MAX_TEMPLATE_NAME:
        return _bad_request("invalid name")
    breed = payload.get("breed_type")
    if breed is not None:
        breed = str(breed).strip() or None
        if breed is not None and len(breed) > svc.MAX_BREED_TYPE:
            return _bad_request("invalid breed_type")

    rules = payload.get("rules")
    error = svc.template_rules_error(rules)
    if error:
        return _bad_request(error)

    farm_id, farm_error = _authoring_farm_id(request, payload.get("farm_id"))
    if farm_error is not None:
        return farm_error

    with db_transaction.atomic():
        template = LifecycleTemplate.objects.create(
            org_id=_org_claim(request),
            farm_id=farm_id,
            name=name,
            breed_type=breed,
            status=LifecycleTemplate.DRAFT,
            created_by=request.user,
            is_default=False,
        )
        _replace_rules(template, rules)
        _template_audit(request, ACTION_TEMPLATE_CREATED, template, rules=len(rules))
    return Response(
        ser.template_detail_item(template, _rules_of(template)),
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    tags=[TAG_OPS],
    operation_id="update_threshold_template",
    summary="O14 — edit a template draft (engineer+, farm-scoped)",
    description="""Partial edit of a **DRAFT**. `rules`, when present, REPLACES the whole band
list (the set is validated for overlap, so a partial band edit could produce a
combination no request contained). Validation is O13's, verbatim.

A **PUBLISHED** template is immutable here → 409: its bands are tuned through
`PUT /thresholds/{rule_id}/`, which audits each change and re-caches
immediately. A foreign or org-NULL (built-in) template → 404. An engineer may
only edit drafts of farms in their grant (org-wide drafts are org-level).

Writes a `THRESHOLD_TEMPLATE_UPDATED` audit row in the same transaction.""",
    parameters=[
        OpenApiParameter(
            "template_id", int, OpenApiParameter.PATH, description="Draft template id."
        )
    ],
    request=ThresholdTemplateUpdateRequestSerializer,
    responses={
        200: ThresholdTemplateDetailSerializer,
        **error_responses(400, 401, 403, 404),
        409: OpenApiResponse(
            description="Template is already published (bands are tuned through O3)."
        ),
    },
)
@api_view(["PUT"])
@permission_classes([IsAuthenticated])
def threshold_template_update(request, template_id: int):
    if not _roles(request) & TEMPLATE_AUTHOR_ROLES:
        return _forbidden()
    template = _own_org_template(request, template_id)
    if template is None:
        return _not_found("template not found")
    scope_error = _authoring_scope_error(request, template)
    if scope_error is not None:
        return scope_error
    if template.status != LifecycleTemplate.DRAFT:
        return _conflict("template is published (tune its rules through /thresholds/)")

    payload = request.data if isinstance(request.data, dict) else {}
    updates = []
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > svc.MAX_TEMPLATE_NAME:
            return _bad_request("invalid name")
        template.name = name
        updates.append("name")
    if "breed_type" in payload:
        breed = payload.get("breed_type")
        breed = str(breed).strip() or None if breed is not None else None
        if breed is not None and len(breed) > svc.MAX_BREED_TYPE:
            return _bad_request("invalid breed_type")
        template.breed_type = breed
        updates.append("breed_type")
    if "rules" in payload:
        error = svc.template_rules_error(payload.get("rules"))
        if error:
            return _bad_request(error)

    with db_transaction.atomic():
        if updates:
            template.save(update_fields=updates)
        if "rules" in payload:
            _replace_rules(template, payload["rules"])
        _template_audit(
            request,
            ACTION_TEMPLATE_UPDATED,
            template,
            changed=sorted(set(payload) & {"name", "breed_type", "rules"}),
        )
    return Response(ser.template_detail_item(template, _rules_of(template)))


@extend_schema(
    tags=[TAG_OPS],
    operation_id="publish_threshold_template",
    summary="O15 — publish a template draft (owner, manager)",
    description="""Makes a draft LIVE: from this moment `effective_template` may resolve it,
so the affected saloons are re-cached in the same request (the publish fires
the alerts cache sweep — the same fast path a rule edit takes).

Optional `is_default: true` also promotes it to the org's default in one call,
demoting the incumbent. Publishing a template whose breed already has a
published org template, or promoting to default while another published
default exists, is a 409 — the uniqueness guards are DB constraints, not
gentlemen's agreements.

404 for a foreign or org-NULL (built-in) template — the shared catalog is
writable by NO ONE, exactly like O3. 409 for an already-published template.
Writes a `THRESHOLD_TEMPLATE_PUBLISHED` audit row in the same transaction.""",
    parameters=[
        OpenApiParameter(
            "template_id", int, OpenApiParameter.PATH, description="Draft template id."
        )
    ],
    request=ThresholdTemplatePublishRequestSerializer,
    responses={
        200: ThresholdTemplateDetailSerializer,
        **error_responses(400, 401, 403, 404),
        409: OpenApiResponse(
            description="Already published, or the breed/default slot is taken."
        ),
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def threshold_template_publish(request, template_id: int):
    if not _roles(request) & TEMPLATE_MANAGE_ROLES:
        return _forbidden()
    template = _own_org_template(request, template_id)
    if template is None:
        return _not_found("template not found")
    if template.status == LifecycleTemplate.PUBLISHED:
        return _conflict("template is already published")
    rules = _rules_of(template)
    if not rules:
        return _bad_request("template has no rules")

    payload = request.data if isinstance(request.data, dict) else {}
    is_default = bool(payload.get("is_default", False))
    try:
        with db_transaction.atomic():
            if is_default:
                # Promote in ONE transaction: demote the incumbent, then save.
                # Order matters — the partial unique index is checked per row.
                LifecycleTemplate.objects.filter(
                    org_id=_org_claim(request),
                    status=LifecycleTemplate.PUBLISHED,
                    is_default=True,
                ).exclude(template_id=template.template_id).update(is_default=False)
            template.status = LifecycleTemplate.PUBLISHED
            template.published_at = timezone.now()
            template.is_default = is_default
            template.save(update_fields=["status", "published_at", "is_default"])
            _template_audit(
                request,
                ACTION_TEMPLATE_PUBLISHED,
                template,
                rules=len(rules),
                is_default=is_default,
            )
    except IntegrityError:
        return _conflict(
            "another published template already covers this breed or is the org default"
        )
    return Response(ser.template_detail_item(template, rules))


@extend_schema(
    methods=["GET"],
    tags=[TAG_OPS],
    operation_id="get_saloon_threshold_template",
    summary="O16 — a saloon's template assignment and its effective template",
    description="""`assigned_template_id` is the explicit assignment (null = inheriting) and
`source` names the resolution step that actually won:
`assigned` | `org_breed` | `org_default` | `global_default` | `none`.

Readable by any member of the owning org. A foreign or unknown saloon is an
identical 404.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        )
    ],
    request=None,
    responses={
        200: SaloonTemplateResponseSerializer,
        **error_responses(401, 403, 404),
    },
)
@extend_schema(
    methods=["PUT"],
    tags=[TAG_OPS],
    operation_id="assign_saloon_threshold_template",
    summary="O16 — assign a template to a saloon (owner, manager)",
    description="""`{"template_id": 7}` assigns; `{"template_id": null}` CLEARS the assignment and
returns the saloon to the breed/default chain. The template must be own-org and
**published** — a draft cannot be assigned (400), and a foreign or org-NULL
built-in template is a 404, never an existence oracle.

The saloon is re-cached immediately, so the new bands are armed without
waiting for the midnight sweep. Writes a `SALOON_TEMPLATE_ASSIGNED` (or
`_CLEARED`) audit row in the same transaction and answers the O16 GET shape.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        )
    ],
    request=SaloonTemplateAssignRequestSerializer,
    responses={
        200: SaloonTemplateResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
)
@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def saloon_threshold_template(request, saloon_id: int):
    """O16 on ONE contractual path; the methods dispatch to the halves below."""
    if request.method == "GET":
        return _saloon_template_read(request, saloon_id)
    return _saloon_template_assign(request, saloon_id)


def _saloon_template_read(request, saloon_id: int):
    if not _org_claim(request):
        return _forbidden()
    saloon = _saloon_row(request, saloon_id)
    if saloon is None:
        return _not_found("saloon not found")
    effective, source = effective_template(_saloon_flock(saloon), saloon)
    return Response(ser.saloon_template_item(saloon, effective, source))


def _saloon_template_assign(request, saloon_id: int):
    if not _roles(request) & TEMPLATE_MANAGE_ROLES:
        return _forbidden()
    saloon = _saloon_row(request, saloon_id)
    if saloon is None:
        return _not_found("saloon not found")

    payload = request.data if isinstance(request.data, dict) else {}
    raw = payload.get("template_id")
    if raw is None:
        template = None  # explicit clear → back to the breed/default chain
    else:
        template_id = _as_int(raw)
        if template_id is None:
            return _bad_request("invalid template_id")
        template = _own_org_template(request, template_id)
        if template is None:
            return _not_found("template not found")
        if template.status != LifecycleTemplate.PUBLISHED:
            return _bad_request("template is not published")

    with db_transaction.atomic():
        saloon.threshold_template = template
        # update_fields= is what the alerts signal keys on (a targeted save
        # that does not touch the assignment must not rebuild the hash).
        saloon.save(update_fields=["threshold_template"])
        AccountActivityLog.objects.create(
            actor=request.user,
            org_id=_org_claim(request),
            farm_id=saloon.farm_id,
            saloon_id=saloon.pk,
            action=ACTION_SALOON_ASSIGNED if template is not None else ACTION_SALOON_CLEARED,
            details={
                "template_id": template.template_id if template is not None else None,
                "template_name": template.name if template is not None else None,
            },
        )
    effective, source = effective_template(_saloon_flock(saloon), saloon)
    return Response(ser.saloon_template_item(saloon, effective, source))


# ---------------------------------------------------------------------------
# Reconciliation — GET /reconciliation/ (v0.9/B5, M9-02 tooling)
# ---------------------------------------------------------------------------


@extend_schema(
    tags=[TAG_OPS],
    operation_id="daily_reconciliation_report",
    summary="Daily delivery ledger: DB rows + DLQ rows + alert list (v0.9/B5)",
    description="""The pilot acceptance ledger per Tehran day: every reading
has a verdict (stored or dead-lettered with a reason). accounted_pct is the
delivery gate (pubacked readings in DB-or-DLQ). Engineer+ reads, claim-org
scoped. ?day=YYYY-MM-DD defaults to yesterday Tehran.""",
    parameters=[
        OpenApiParameter(
            "day", str, OpenApiParameter.QUERY, required=False,
            description="Tehran day, YYYY-MM-DD (default: yesterday).",
        )
    ],
    responses={
        200: ser.ReconReportSerializer,
        **error_responses(400, 401, 403),
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def reconciliation(request):
    """GET /reconciliation/ — the shadow-run daily loop in one call."""
    org_claim = _org_claim(request)
    if org_claim is None:
        return _forbidden()
    if not _roles(request) & {"owner", "manager", "engineer"}:
        return _forbidden()
    raw_day = str(request.query_params.get("day") or "").strip()
    try:
        day = svc.parse_recon_day(raw_day) if raw_day else svc.tehran_yesterday()
    except ValueError:
        return _bad_request("day must be YYYY-MM-DD")
    return Response(svc.recon_day_report(day, org_id=org_claim))
