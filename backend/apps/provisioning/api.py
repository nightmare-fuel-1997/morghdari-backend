"""Gateway request and review API for v0.5."""

from __future__ import annotations

import base64
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.tenants.context import admin_alias
from apps.common.schemas import (
    CONFLICT_RESPONSE,
    RATE_LIMITED_RESPONSE,
    TAG_PROVISIONING,
    ErrorEnvelopeSerializer,
    error_responses,
)
from apps.devices.models import (
    GatewayHardwareRegistry,
    GatewayRequest,
    GatewaySaloonZone,
    GatewaySensorMapping,
    GatewaySlot,
    SaloonSensorSlot,
)
from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
from apps.provisioning.serializers import (
    CredentialRotationResponseSerializer,
    GatewayActionRequestSerializer,
    GatewayConfigResponseSerializer,
    GatewayRequestCreateSerializer,
    GatewayRequestSerializer,
    GatewayReviewApprovedSerializer,
    GatewayReviewRequestSerializer,
    GatewayStatusResponseSerializer,
    GeoCitySerializer,
    GeoProvinceSerializer,
    GeoVillageSerializer,
    LocationTreeProvinceSerializer,
    ProvisioningSyncRequestSerializer,
    ProvisioningSyncResponseSerializer,
    RotationAckRequestSerializer,
    RotationAckResponseSerializer,
    SaloonPositionSerializer,
)
from apps.provisioning.services import (
    SyncConflict,
    acquire_sync_lock,
    release_sync_lock,
    transition,
)
from apps.system_config.models import SystemConfiguration


def gateway_acl_rule_items(org_uuid, farm_id: int, slot_id: str) -> list[dict]:
    """The gateway's FULL MQTT allow-rule set (approval-time, M5-07).

    v0.8 M8-04-H (C5): the firmware contract needs more than telemetry/status
    publish — the downlink topics (commands T3, clock/sync T5, ota T6,
    diagnostics/request T8) and the uplink responses (commands/response T4,
    ota/response T7, diagnostics/response T9). Under `no_match=deny` a rule
    makes the topic; without these the real firmware is denied at the broker.
    فارس: مجموعهٔ کاملِ قواعدِ MQTT گیت‌وی — فقط با این‌ها فریم‌ویر واقعی می‌تواند
    زیرِ no_match=deny روی تاپیک‌های فرمان/ساعت/OTA/تشخیص پاسخ دهد.
    """
    base = f"{org_uuid}/farms/{farm_id}/gateways/{slot_id}"
    publishes = [
        ("telemetry", "T1"), ("status", "T2"),
        ("commands/response", "T4"), ("ota/response", "T7"),
        ("diagnostics/response", "T9"),
    ]
    subscribes = [
        ("commands", "T3"), ("clock/sync", "T5"), ("ota", "T6"),
        ("diagnostics/request", "T8"),
    ]
    items = [{"action": "publish", "topic": f"{base}/{suffix}"} for suffix, _ in publishes]
    items += [{"action": "subscribe", "topic": f"{base}/{suffix}"} for suffix, _ in subscribes]
    return [{"permission": "allow", **item} for item in items]

User = get_user_model()
REVIEW_PERMISSION = "gateway_review"
REVIEW_ROLES = {"admin", "support"}
MANAGER_ROLES = {"manager", "owner"}


def _org_id(request):
    return _claims(request).get("org_id")


def _can_review(request) -> bool:
    user = request.user
    if user.is_superuser:
        return True
    if user.is_platform_staff and getattr(user, "staff_role", None) in REVIEW_ROLES:
        return True
    # Keep the extension point data-driven when a deployment adds a platform
    # permission model; deployments without that optional model use defaults.
    try:
        from apps.accounts.models import PlatformPermission
    except ImportError:
        return False
    return PlatformPermission.objects.filter(user=user, codename=REVIEW_PERMISSION).exists()


def _config_int(key: str, default: int) -> int:
    row = SystemConfiguration.objects.filter(pk=key).values_list("value", flat=True).first()
    try:
        return int(row) if row is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# M6-01 geography reads (contract v1.5.0 — installer drill-down surface)
# ---------------------------------------------------------------------------


def _geo_out_province(row):
    return {"id": row.id, "name": row.name}


def _geo_out_city(row):
    return {"id": row.id, "province_id": row.province_id, "name": row.name}


def _geo_out_village(row):
    return {"id": row.id, "city_id": row.city_id, "name": row.name}


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="list_provinces",
    summary="Provinces (global reference data)",
    description="""The first step of the installer's geography drill-down. Global
reference data — not org-scoped, no RLS — so every authenticated caller sees
the same rows. Bare list, alphabetical by name.""",
    request=None,
    responses={200: GeoProvinceSerializer(many=True), **error_responses(401)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def province_list(request):
    """GET /provisioning/provinces/ — all provinces (global reference data)."""
    from apps.geography.models import Province

    return Response([_geo_out_province(p) for p in Province.objects.all().order_by("name")])


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="list_cities",
    summary="Cities of one province",
    description="""Step two of the geography drill-down. `province_id` is REQUIRED: a missing
or non-numeric value is a 400 `province_id query parameter is required` rather
than an unfiltered dump of every city. Bare list, alphabetical by name.""",
    parameters=[
        OpenApiParameter(
            "province_id",
            int,
            OpenApiParameter.QUERY,
            required=True,
            description="Province id from the province list.",
        )
    ],
    request=None,
    responses={200: GeoCitySerializer(many=True), **error_responses(400, 401)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def city_list(request):
    """GET /provisioning/cities/?province_id= — cities of one province."""
    from apps.geography.models import City

    try:
        province_id = int(request.query_params.get("province_id", ""))
    except (TypeError, ValueError):
        return Response({"error": "province_id query parameter is required"}, status=400)
    rows = City.objects.filter(province_id=province_id).order_by("name")
    return Response([_geo_out_city(c) for c in rows])


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="list_villages",
    summary="Villages of one city",
    description="""Final geography step; villages are optional in the address model, so an
empty list is a normal answer. `city_id` is REQUIRED — missing or non-numeric
is a 400 `city_id query parameter is required`.""",
    parameters=[
        OpenApiParameter(
            "city_id",
            int,
            OpenApiParameter.QUERY,
            required=True,
            description="City id from the city list.",
        )
    ],
    request=None,
    responses={200: GeoVillageSerializer(many=True), **error_responses(400, 401)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def village_list(request):
    """GET /provisioning/villages/?city_id= — villages of one city."""
    from apps.geography.models import Village

    try:
        city_id = int(request.query_params.get("city_id", ""))
    except (TypeError, ValueError):
        return Response({"error": "city_id query parameter is required"}, status=400)
    rows = Village.objects.filter(city_id=city_id).order_by("name")
    return Response([_geo_out_village(v) for v in rows])


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="list_saloon_positions",
    summary="Sensor positions of a saloon",
    description="""Installer reference rows (id + label) used when mapping sensor packs to
physical positions. Scoped to the caller's org claim; a caller without an org
claim gets an empty list rather than a cross-tenant read.""",
    parameters=[
        OpenApiParameter(
            "saloon_id", int, OpenApiParameter.PATH, description="Saloon id (caller's org)."
        )
    ],
    request=None,
    responses={200: SaloonPositionSerializer(many=True), **error_responses(401)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_positions(request, saloon_id: int):
    """GET /provisioning/saloons/{saloon_id}/positions/ — sensor positions."""
    from apps.devices.models import SaloonPosition

    org_id = _org_id(request)
    if org_id:
        rows = SaloonPosition.objects.filter(org_id=org_id, saloon_id=saloon_id).order_by("id")
        return Response([
            {"id": r.id, "saloon_id": r.saloon_id, "label": r.label} for r in rows
        ])
    return Response([], status=200)


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="get_location_tree",
    summary="Installer location tree (province → city → villages + farms)",
    description="""One cached payload for the whole installer drill-down: geography is global
reference data, and the **only** org-scoped nodes are the farm leaves under
each city (`farm_id`, `name`) — they carry the caller's `org_id` claim and
therefore never leak across tenants.

The response is cached for one hour under a per-org key (`location:tree:{org}`),
so a farm rename cannot bleed into another org's tree; a cache outage degrades
to a fresh read, never an error. No org claim → 400.""",
    request=None,
    responses={
        200: LocationTreeProvinceSerializer(many=True),
        **error_responses(400, 401),
    },
    examples=[
        OpenApiExample(
            "Tree with farm leaves",
            response_only=True,
            value=[
                {
                    "id": 8,
                    "name": "Tehran",
                    "cities": [
                        {
                            "id": 31,
                            "name": "Varamin",
                            "villages": [{"id": 102, "name": "Qeshlaq"}],
                            "farms": [{"farm_id": 7, "name": "Golden Egg North"}],
                        }
                    ],
                }
            ],
        )
    ],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def location_tree(request):
    """GET /provisioning/location-tree/ — installer drill-down: province →
    cities (+ villages) → the caller org's farms in each city.

    Geography is global reference data; the FARM leaves are the only org
    data, and they are scoped to the caller's org_id claim. Server-cached 1 h
    (django cache — redis in prod, locmem in dev/test), keyed per org so a
    farm rename never leaks across tenants.
    فارسی: درختِ مکان برای نصاب — جغرافیا سراسری است و فقط برگ‌های مزرعه به
    سازمانِ تماس‌گیرنده محدود می‌شود؛ پاسخ یک ساعت کش می‌شود.
    """
    from django.core.cache import cache

    from apps.farms.models import Farm
    from apps.geography.models import City, Province, Village

    org_id = _org_id(request)
    if org_id is None:
        return Response({"error": "organization context required"}, status=400)

    cache_key = f"location:tree:{org_id}"
    cached = None
    try:
        cached = cache.get(cache_key)
    except Exception:  # noqa: BLE001 — cache outage degrades to a fresh read
        cached = None
    if cached is not None:
        return Response(cached)

    # Farm leaves per city (caller org only).
    farms_by_city = {}
    for farm in (
        Farm.objects.filter(org_id=org_id)
        .only("id", "name", "city_id")
        .order_by("city_id")
    ):
        farms_by_city.setdefault(farm.city_id, []).append(
            {"farm_id": farm.id, "name": farm.name}
        )

    villages_by_city = {}
    for village in Village.objects.all().order_by("city_id", "name"):
        villages_by_city.setdefault(village.city_id, []).append(
            {"id": village.id, "name": village.name}
        )

    cities_by_province = {}
    for city in City.objects.select_related("province").all().order_by("province_id", "name"):
        cities_by_province.setdefault(city.province_id, []).append({
            "id": city.id,
            "name": city.name,
            "villages": villages_by_city.get(city.id, []),
            "farms": farms_by_city.get(city.id, []),
        })

    tree = [
        {
            "id": province.id,
            "name": province.name,
            "cities": cities_by_province.get(province.id, []),
        }
        for province in Province.objects.all().order_by("name")
    ]
    try:
        cache.set(cache_key, tree, timeout=3600)
    except Exception:  # noqa: BLE001 — never fail a read because caching did
        pass
    return Response(tree)


def _request_out(row):
    return {
        "id": row.id,
        "org_id": str(row.org_id),
        "farm_id": row.farm_id,
        "status": row.status,
        "requested_by": row.requested_by_id,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "approved_by": row.approved_by_id,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "rejection_reason": row.rejection_reason,
        "details": row.details,
    }


@extend_schema(
    methods=["GET"],
    tags=[TAG_PROVISIONING],
    operation_id="list_gateway_requests",
    summary="List gateway requests",
    description="""Bare list of the org's gateway requests, oldest first. A platform reviewer
sees the org named by their claims; a caller with neither a review permission
nor an org claim gets a 400 rather than an unscoped read.""",
    request=None,
    responses={200: GatewayRequestSerializer(many=True), **error_responses(400, 401)},
)
@extend_schema(
    methods=["POST"],
    tags=[TAG_PROVISIONING],
    operation_id="create_gateway_request",
    summary="Submit a gateway request",
    description="""Farm manager/owner only (403 otherwise). Requests are **capped**
per org: `max_pending_gateway_requests` (default 4) PENDING rows, and the
next one is a 400 `pending gateway request cap reached` — the cap exists so an
org cannot flood the review queue.

The requester is always the JWT user; `details` is stored verbatim as free-form
context. Approval of the request is what mints the gateway slot and its MQTT
credential.""",
    request=GatewayRequestCreateSerializer,
    responses={
        201: GatewayRequestSerializer,
        **error_responses(400, 401, 403),
    },
    examples=[
        OpenApiExample(
            "Request a gateway",
            request_only=True,
            value={"farm_id": 7, "details": {"install_window": "2026-09-12 morning", "contact": "Ali"}},
        )
    ],
)
@api_view(["POST", "GET"])
@permission_classes([IsAuthenticated])
def gateway_requests(request):
    """Submit or list requests within the caller's organization."""
    org_id = _org_id(request)
    if request.method == "GET":
        if not _can_review(request) and not org_id:
            return Response({"error": "organization context required"}, status=400)
        queryset = GatewayRequest.objects.filter(org_id=org_id).order_by("id")
        return Response([_request_out(row) for row in queryset])

    if not org_id or not (MANAGER_ROLES & set(_claims(request).get("roles", []))):
        return Response({"error": "manager role required"}, status=403)

    pending = GatewayRequest.objects.filter(org_id=org_id, status="PENDING").count()
    if pending >= _config_int("max_pending_gateway_requests", 4):
        return Response({"error": "pending gateway request cap reached"}, status=400)

    row = GatewayRequest.objects.create(
        org_id=org_id,
        farm_id=request.data.get("farm_id"),
        requested_by=request.user,
        details=request.data.get("details") or {},
    )
    return Response(_request_out(row), status=201)


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="review_gateway_request",
    summary="Approve or reject a gateway request",
    description="""Platform review permission required (superuser, `admin`/`support` staff
role, or the `gateway_review` platform permission). The row is locked
(`select_for_update`) for the whole decision, so two reviewers cannot both win.

**REJECT** writes the reason (default `rejected`) and returns the request row.
**APPROVE** mints a `GatewaySlot` (supplied `slot_id` or a generated `GW-…`),
a bcrypt MQTT credential, publish-only EMQX allow rules for the gateway's own
`telemetry`/`status` topics, and registers the broker authn user — then returns
the request plus `gateway_slot_id`, `username` and the **one-time plaintext
`password`**. A broker hiccup never fails the review (FNV-soft; the next
rotation heals it).

Verdicts: 404 unknown request; **409** `request already reviewed` when the row
is no longer PENDING; 400 for a decision other than APPROVE/REJECT. Approving a
slot id that already exists is a 409 from the unique constraint.""",
    parameters=[
        OpenApiParameter(
            "request_id", int, OpenApiParameter.PATH, description="Gateway request id."
        )
    ],
    request=GatewayReviewRequestSerializer,
    responses={
        200: GatewayReviewApprovedSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Approve",
            request_only=True,
            value={"decision": "APPROVE", "slot_id": "GW-0041"},
        )
    ],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def review_gateway_request(request, request_id: int):
    """Approve/reject a request; approval creates credentials and a slot."""
    if not _can_review(request):
        return Response({"error": "platform review permission required"}, status=403)

    with transaction.atomic():
        row = GatewayRequest.objects.select_for_update().filter(pk=request_id).first()
        if row is None:
            return Response({"error": "request not found"}, status=404)
        if row.status != "PENDING":
            return Response({"error": "request already reviewed"}, status=409)

        decision = str(request.data.get("decision", "")).upper()
        if decision == "REJECT":
            row.status = "REJECTED"
            row.rejection_reason = str(request.data.get("reason", "")).strip() or "rejected"
            row.approved_by = request.user
            row.approved_at = timezone.now()
            row.save(update_fields=["status", "rejection_reason", "approved_by", "approved_at"])
            return Response(_request_out(row))
        if decision != "APPROVE":
            return Response({"error": "decision must be APPROVE or REJECT"}, status=400)

        slot_id = str(request.data.get("slot_id") or f"GW-{secrets.token_hex(8).upper()}")
        password = secrets.token_urlsafe(32)
        slot = GatewaySlot.objects.create(
            slot_id=slot_id,
            org=row.org,
            farm=row.farm,
            request=row,
            created_by=request.user,
        )
        from apps.mqtt_auth.services import ensure_gateway_user, hash_password

        credential = MqttDeviceCredential.objects.create(
            device_id=slot.slot_id,
            username=slot.slot_id,
            password_hash=hash_password(password),  # $2b$ — EMQX-native (C7)
        )
        # Allow rules MUST name the topics the gateway actually uses
        # (`{org}/farms/{farm}/gateways/{slot}/...` — the subscriber's grammar,
        # verified against the live broker; the old `org/.../gateway/#` shape
        # matched nothing and no_match=deny dropped every publish). M8-04-H:
        # the full firmware contract — telemetry/status publish PLUS the
        # downlink subscriptions (T3/T5/T6/T8) and response publishes (T4/T7/T9).
        for rule in gateway_acl_rule_items(str(row.org_id), slot.farm_id, slot.slot_id):
            MqttAclRule.objects.create(
                username=credential.username,
                topic=rule["topic"],
                permission=rule["permission"],
                action=rule["action"],
            )
        # The broker's authn user — plaintext is in hand exactly here (one-time
        # issue). FNV-soft: a broker hiccup never fails the review; a missing
        # user is healed by the next credential rotation (404-heal in
        # mqtt_auth.services.update_gateway_user_password).
        ensure_gateway_user(slot.slot_id, password)
        row.status = "APPROVED"
        row.approved_by = request.user
        row.approved_at = timezone.now()
        row.save(update_fields=["status", "approved_by", "approved_at"])

    return Response({**_request_out(row), "gateway_slot_id": slot.slot_id, "username": credential.username, "password": password}, status=200)


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="provisioning_sync",
    summary="Installer topology sync (moves the gateway to PENDING_APPROVAL)",
    description="""**Installer role only.** Organization and hardware identity always come from
server-side rows — the body names a slot in the caller's org, never topology
of its own invention. A short Redis lock guards against two offline installers
merging overlapping topology: a held lock is a **409**
`gateway sync already in progress` with `Retry-After: 3`.

All three lists (`zones`, `sensor_slot_mappings`, `replacements`) are validated
UP FRONT, so a malformed entry can never leave half-written topology (M5-09):
an unknown hardware/sensor reference is a 400 and nothing is written.
`zones` accepts plain saloon ids; assignments are idempotent.

The sync ends by transitioning the slot to **PENDING_APPROVAL** and returns the
counts it applied.""",
    request=ProvisioningSyncRequestSerializer,
    responses={
        200: ProvisioningSyncResponseSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Sync two saloons and one pack",
            request_only=True,
            value={
                "gateway_slot_id": "GW-0041",
                "zones": [12, 13],
                "sensor_slot_mappings": [{"pack_id": "PACK-A", "slot_id": "SS-01"}],
                "replacements": [],
            },
        )
    ],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def provisioning_sync(request):
    """Apply installer topology and move the gateway to PENDING_APPROVAL.

    Organization and hardware identity come from server-side records. The
    short Redis lock prevents two offline installers from merging overlapping
    topology updates at the same time.
    """
    roles = set(_claims(request).get("roles", []))
    org_id = _org_id(request)
    if not org_id or "installer" not in roles:
        return Response({"error": "installer role required"}, status=403)

    slot_id = str(request.data.get("gateway_slot_id") or "")
    if not slot_id:
        return Response({"error": "gateway_slot_id required"}, status=400)

    try:
        lock_client, lock_token = acquire_sync_lock(slot_id)
    except SyncConflict:
        response = Response({"error": "gateway sync already in progress"}, status=409)
        response["Retry-After"] = str(3)
        return response

    try:
        with transaction.atomic():
            slot = GatewaySlot.objects.select_for_update().filter(
                pk=slot_id, org_id=org_id
            ).first()
            if slot is None:
                return Response({"error": "gateway not found in caller organization"}, status=404)

            hardware = GatewayHardwareRegistry.objects.filter(
                slot=slot, org_id=org_id, removed_at__isnull=True
            ).order_by("-installed_at").first()
            if hardware is None:
                return Response({"error": "active gateway hardware not registered"}, status=400)

            zones = request.data.get("zones") or request.data.get("zone_assignments") or []
            mappings = request.data.get("sensor_slot_mappings") or []

            # M5-03 replacements — a hardware unit swap on this gateway slot:
            # retire the OLD registry row, re-point `pack_id` to a (possibly
            # new) sensor slot under the NEW hardware, and drop the stale pack
            # mapping left under the old unit. Server-side truth everywhere:
            # both hardware rows must be active rows of THIS slot in the
            # caller's org — an unknown row is a 400, never a silent no-op.
            # VALIDATED UP FRONT (before zones/mappings write anything) so a
            # malformed list can never yield partial topology (M5-09).
            replacements = request.data.get("replacements") or []
            replacement_plan = []
            for item in replacements:
                old_hw = str(item.get("old_hardware_id") or "")
                new_hw = str(item.get("new_hardware_id") or "")
                pack_id = str(item.get("pack_id") or "")
                sensor_slot_id = str(item.get("slot_id") or "")
                if not (old_hw and new_hw and pack_id and sensor_slot_id):
                    return Response(
                        {"error": "replacement requires old_hardware_id, new_hardware_id, pack_id and slot_id"},
                        status=400,
                    )
                old_active = GatewayHardwareRegistry.objects.filter(
                    hardware_id=old_hw, slot=slot, org_id=org_id, removed_at__isnull=True
                ).first()
                new_active = GatewayHardwareRegistry.objects.filter(
                    hardware_id=new_hw, slot=slot, org_id=org_id, removed_at__isnull=True
                ).first()
                sensor_slot = SaloonSensorSlot.objects.filter(
                    pk=sensor_slot_id, org_id=org_id, is_active=True
                ).first()
                if old_active is None or new_active is None or sensor_slot is None:
                    return Response(
                        {"error": "replacement references unknown hardware or sensor slot"},
                        status=400,
                    )
                replacement_plan.append(
                    (old_active, new_active, pack_id, sensor_slot)
                )

            for saloon_id in zones:
                zone = GatewaySaloonZone.objects.filter(
                    gateway_slot=slot, saloon_id=saloon_id, org_id=org_id
                ).first()
                if zone is None:
                    GatewaySaloonZone.objects.create(
                        gateway_slot=slot, saloon_id=saloon_id, org_id=org_id,
                        assigned_by=request.user,
                    )

            for item in mappings:
                pack_id = str(item.get("pack_id") or "")
                sensor_slot_id = str(item.get("slot_id") or "")
                if not pack_id or not sensor_slot_id:
                    return Response({"error": "mapping requires pack_id and slot_id"}, status=400)
                sensor_slot = SaloonSensorSlot.objects.filter(
                    pk=sensor_slot_id, org_id=org_id, is_active=True
                ).first()
                if sensor_slot is None:
                    return Response({"error": "sensor slot not found in caller organization"}, status=400)
                GatewaySensorMapping.objects.update_or_create(
                    gateway_hardware_id=hardware.hardware_id,
                    pack_id=pack_id,
                    defaults={"slot": sensor_slot, "org_id": org_id, "created_by": request.user},
                )

            now = timezone.now()
            for old_active, new_active, pack_id, sensor_slot in replacement_plan:
                if old_active.pk != new_active.pk:
                    old_active.removed_at = now
                    old_active.removed_by = request.user
                    old_active.save(update_fields=["removed_at", "removed_by"])
                    # The retired unit's mapping for this pack is stale.
                    GatewaySensorMapping.objects.filter(
                        gateway_hardware_id=old_active.hardware_id,
                        pack_id=pack_id,
                    ).delete()
                GatewaySensorMapping.objects.update_or_create(
                    gateway_hardware_id=new_active.hardware_id,
                    pack_id=pack_id,
                    defaults={"slot": sensor_slot, "org_id": org_id, "created_by": request.user},
                )

            transition(slot.slot_id, "PENDING_APPROVAL", request.user, reason="installer sync")
            return Response({
                "gateway_slot_id": slot.slot_id,
                "gateway_hardware_id": hardware.hardware_id,
                "status": "PENDING_APPROVAL",
                "zones_count": len(zones),
                "mappings_count": len(mappings),
                "replacements_count": len(replacement_plan),
            }, status=200)
    finally:
        release_sync_lock(slot_id, lock_token, client=lock_client)


# ---------------------------------------------------------------------------
# M5-05 — farm-manager lifecycle: approve / reject / suspend / resume.
# The transaction + audit row come from the shared transition service; each
# action's broker-consistency step (pending drain, DLQ discard, EMQX ACL
# sync/revoke) runs AFTER the status commit, so a broker hiccup never rolls
# the decision back — the next sweep/appointment converges it.
# ---------------------------------------------------------------------------


def _manager_or_error(request):
    """Return the org id when the caller holds a manager/owner role, else None.
    Every M5-05/06/08 gateway action starts with this gate."""
    org_id = _org_id(request)
    if not org_id or not (MANAGER_ROLES & set(_claims(request).get("roles", []))):
        return None
    return org_id


def _owned_slot(slot_id: str, org_id: str):
    return GatewaySlot.objects.filter(pk=slot_id, org_id=org_id).first()


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="approve_gateway",
    summary="Approve a PENDING_APPROVAL gateway (PROVISIONED)",
    description="""Farm manager/owner only, and only for a gateway of the caller's org
(anything else is the same 404 `gateway not found in caller organization`).

Approval transitions the slot to **PROVISIONED** and drains the telemetry it
held while pending (FIFO), then syncs the EMQX allow rules. The broker step
runs AFTER the status commit, so a broker hiccup never rolls the decision
back — the next sweep converges it. A wrong starting status is a 409.""",
    parameters=[
        OpenApiParameter(
            "gateway_slot_id", str, OpenApiParameter.PATH, description="Gateway slot id (caller's org)."
        )
    ],
    request=None,
    responses={
        200: GatewayStatusResponseSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(401, 403, 404),
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def approve_gateway(request, gateway_slot_id: str):
    """Approve a PENDING_APPROVAL gateway: PROVISIONED + held telemetry drain
    (FIFO), then EMQX allow-rule sync. Farm manager/owner only."""
    org_id = _manager_or_error(request)
    if org_id is None:
        return Response({"error": "manager role required"}, status=403)
    slot = _owned_slot(gateway_slot_id, org_id)
    if slot is None:
        return Response({"error": "gateway not found in caller organization"}, status=404)

    from apps.provisioning.services import approve_gateway as _approve

    try:
        _approve(slot.slot_id, request.user)
    except ValueError as exc:  # wrong starting status
        return Response({"error": str(exc)}, status=409)
    slot = GatewaySlot.objects.get(pk=gateway_slot_id)
    return Response({"gateway_slot_id": slot.slot_id, "status": slot.provisioning_status})


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="reject_gateway",
    summary="Reject a PENDING_APPROVAL gateway",
    description="""Farm manager/owner only. Moves the slot to **REJECTED**, archives the
telemetry it held to the DLQ with the given `reason`, and removes its EMQX ACL
rules. Optional body `reason` (free text) is recorded with the archive.

A gateway that is not PENDING_APPROVAL is a 409 from the state matrix — the
lifecycle is not a free-form status write.""",
    parameters=[
        OpenApiParameter(
            "gateway_slot_id", str, OpenApiParameter.PATH, description="Gateway slot id (caller's org)."
        )
    ],
    request=GatewayActionRequestSerializer,
    responses={
        200: GatewayStatusResponseSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(401, 403, 404),
    },
    examples=[OpenApiExample("Reject with reason", request_only=True, value={"reason": "Wiring photos missing"})],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reject_gateway(request, gateway_slot_id: str):
    """Reject a PENDING_APPROVAL gateway: REJECTED + held telemetry archived
    to the DLQ with a reason + EMQX ACL removal."""
    org_id = _manager_or_error(request)
    if org_id is None:
        return Response({"error": "manager role required"}, status=403)
    slot = _owned_slot(gateway_slot_id, org_id)
    if slot is None:
        return Response({"error": "gateway not found in caller organization"}, status=404)

    from apps.provisioning.services import reject_gateway as _reject

    try:
        _reject(slot.slot_id, request.user, reason=str(request.data.get("reason", "")).strip() or None)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=409)
    slot = GatewaySlot.objects.get(pk=gateway_slot_id)
    return Response({"gateway_slot_id": slot.slot_id, "status": slot.provisioning_status})


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="suspend_gateway",
    summary="Suspend a PROVISIONED gateway",
    description="""Farm manager/owner only. Moves the slot to **SUSPENDED** and revokes its
EMQX ACL rules: with `no_match = deny`, every publish stops immediately until
a resume. The broker revocation happens after the commit and converges on the
next sweep if the broker was unreachable.""",
    parameters=[
        OpenApiParameter(
            "gateway_slot_id", str, OpenApiParameter.PATH, description="Gateway slot id (caller's org)."
        )
    ],
    request=GatewayActionRequestSerializer,
    responses={
        200: GatewayStatusResponseSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(401, 403, 404),
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def suspend_gateway(request, gateway_slot_id: str):
    """Suspend a PROVISIONED gateway: SUSPENDED + EMQX ACL revocation
    (no_match=deny drops every publish until resume)."""
    org_id = _manager_or_error(request)
    if org_id is None:
        return Response({"error": "manager role required"}, status=403)
    slot = _owned_slot(gateway_slot_id, org_id)
    if slot is None:
        return Response({"error": "gateway not found in caller organization"}, status=404)

    from apps.provisioning.services import suspend_gateway as _suspend

    try:
        _suspend(slot.slot_id, request.user, reason=str(request.data.get("reason", "")).strip() or None)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=409)
    slot = GatewaySlot.objects.get(pk=gateway_slot_id)
    return Response({"gateway_slot_id": slot.slot_id, "status": slot.provisioning_status})


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="resume_gateway",
    summary="Resume a SUSPENDED gateway",
    description="""Farm manager/owner only. Restores the prior status (**PROVISIONED**) and
re-syncs the EMQX allow rules, so publishes flow again without a reinstall.
Only a SUSPENDED gateway can be resumed — anything else is a 409.""",
    parameters=[
        OpenApiParameter(
            "gateway_slot_id", str, OpenApiParameter.PATH, description="Gateway slot id (caller's org)."
        )
    ],
    request=GatewayActionRequestSerializer,
    responses={
        200: GatewayStatusResponseSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(401, 403, 404),
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resume_gateway(request, gateway_slot_id: str):
    """Resume a SUSPENDED gateway back to PROVISIONED and restore EMQX ACLs."""
    org_id = _manager_or_error(request)
    if org_id is None:
        return Response({"error": "manager role required"}, status=403)
    slot = _owned_slot(gateway_slot_id, org_id)
    if slot is None:
        return Response({"error": "gateway not found in caller organization"}, status=404)

    from apps.provisioning.services import resume_gateway as _resume

    try:
        _resume(slot.slot_id, request.user)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=409)
    slot = GatewaySlot.objects.get(pk=gateway_slot_id)
    return Response({"gateway_slot_id": slot.slot_id, "status": slot.provisioning_status})


# ---------------------------------------------------------------------------
# M5-06 — gateway config fetch (R2 contract payload). Works pre- AND
# post-approval; a rejected or suspended gateway gets 403 (its traffic is
# denied, so its config must not be refreshed either).
#
# Two auth paths, by design (TASKS M5-06 "Basic-auth vs bcrypt creds"):
#   ① the GATEWAY proves itself with HTTP Basic against its own
#      mqtt_device_credentials bcrypt hash (zero-touch bootstrapping);
#   ② a farm manager/owner fetches with the normal JWT.
# Pre-approval the gateway has no credential row yet, so ① only exists
# post-approval — the manager path covers the pre-approval fetch.
# ---------------------------------------------------------------------------


def _basic_device(request):
    """Resolve a gateway identity from an HTTP Basic header against its bcrypt
    credential hash. Returns the MqttDeviceCredential row or None (bad/missing
    credentials). Never raises for malformed headers — fail closed."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    if not username or not password:
        return None

    from apps.mqtt_auth.models import MqttDeviceCredential
    from apps.mqtt_auth.services import verify_password

    credential = MqttDeviceCredential.objects.filter(username=username).first()
    if credential is None or not verify_password(password, credential.password_hash):
        return None
    return credential


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="get_gateway_config",
    summary="Zero-touch provisioning — gateway fetches its configuration",
    description="""Two accepted identities, resolved **inside** the view because the two
callers use different auth systems:

① the gateway itself, HTTP Basic (`username = gateway_slot_id`, password =
its current MQTT credential, validated against the bcrypt hash) — available
only post-approval, since that is when a credential row exists; or② a farm
manager/owner with a normal Bearer JWT.

Returns R2 contract-safe config: org/farm identity, MQTT settings, and the
credential *shape* (`password_hash`, `rotation_pending`). The plaintext
password is never echoed here — it is issued exactly once at approval or
rotation. Deliberately no sensor slot ids in the payload.

Verdicts: 401 with neither identity; 403 for a non-manager JWT, or for a
REJECTED/SUSPENDED gateway (its traffic is denied, so its config is locked
too); 404 for an unknown slot — and for a valid device identity asking about a
different gateway (a probe gets the same 404). Rate-limited at the proxy
(1 r/s per IP).""",
    parameters=[
        OpenApiParameter(
            "gateway_slot_id", str, OpenApiParameter.PATH, description="Gateway slot id."
        )
    ],
    # `auth` lists the alternatives the view accepts; {} documents the
    # anonymous probe path that answers 401 (never a 500).
    auth=[{"GatewayBasicAuth": []}, {"BearerAuth": []}, {}],
    request=None,
    responses={
        200: GatewayConfigResponseSerializer,
        401: ErrorEnvelopeSerializer,
        403: ErrorEnvelopeSerializer,
        404: ErrorEnvelopeSerializer,
        429: RATE_LIMITED_RESPONSE,
    },
    examples=[
        OpenApiExample(
            "Gateway config",
            response_only=True,
            value={
                "gateway_slot_id": "GW-0041",
                "org_id": "1f0c3a54-6f2b-4c9d-9d5e-2b7f4a1c8e30",
                "farm_id": 7,
                "provisioning_status": "PROVISIONED",
                "mqtt": {"host": "mqtt.morghdari.com", "port": 8883, "tls": True},
                "auth": {
                    "username": "GW-0041",
                    "password_hash": "$2b$12$…",
                    "password_issued_once": True,
                    "rotation_pending": False,
                },
                "rate_limit_note": "Throttle MQTT CONNECT + publish rate per gateway at the reverse-proxy layer.",
            },
        )
    ],
)
@api_view(["GET"])
@permission_classes([AllowAny])
def gateway_config(request, gateway_slot_id: str):
    """GET /gateways/{id}/config/ — org/farm identity + MQTT endpoint +
    credential shape. Never echoes the one-time plaintext password.
    Authentication is resolved IN the view (device Basic vs manager JWT)
    because the two callers use different auth systems."""
    # BUG-11 (QA 12.7): `gateway_slots` is FORCE-RLS'd, so a context-free
    # lookup as the app role hides the row and EVERY non-JWT caller - a valid
    # device Basic credential included - got 404 "gateway not found", which
    # made the documented 401 unreachable. Discovery runs on the sanctioned
    # admin alias (same pattern as replay.py / monitoring); authorization is
    # unchanged below, so a caller must still prove the device credential or
    # the manager/owner JWT org claim.
    # فارسی: کشفِ اسلات با alias ادمین تا RLS آن را پنهان نکند؛ احراز هویت
    # همچنان در همین ویو و بدون تغییر اعمال میشود.
    slot = GatewaySlot.objects.using(admin_alias()).filter(pk=gateway_slot_id).first()
    if slot is None:
        return Response({"error": "gateway not found"}, status=404)

    from apps.mqtt_auth.models import MqttDeviceCredential
    from apps.mqtt_auth.services import gateway_config_payload

    credential = MqttDeviceCredential.objects.filter(username=slot.slot_id).first()
    device = _basic_device(request)
    if device is None:
        # Manager/owner JWT path.
        claims = _claims(request)
        org_id = claims.get("org_id") if claims else None
        roles = set(claims.get("roles", [])) if claims else set()
        if org_id is None and not request.user.is_authenticated:
            return Response(
                {"error": "gateway Basic credentials or manager role required"},
                status=401,
            )
        if not (MANAGER_ROLES & roles):
            return Response({"error": "manager role required"}, status=403)
        if str(slot.org_id) != str(org_id):  # UUID vs claim-string comparison
            return Response({"error": "gateway not found in caller organization"}, status=404)
    elif device.username != slot.slot_id:
        # A valid device identity asking about ANOTHER gateway is a probe.
        return Response({"error": "gateway not found"}, status=404)

    if slot.provisioning_status in ("REJECTED", "SUSPENDED"):
        return Response(
            {"error": f"gateway {slot.provisioning_status.lower()} — config locked"},
            status=403,
        )
    return Response(gateway_config_payload(slot, credential))


# ---------------------------------------------------------------------------
# M5-08 — staged credential rotation (stub for the v0.3 command channel).
# stage: new hash into pending (old stays active — no lockout); ack: promote
# the pending hash ONLY when the device presents the new password. The
# plaintext is returned exactly once, at stage time.
# ---------------------------------------------------------------------------


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="rotate_gateway_credential",
    summary="Stage a credential rotation (one-time plaintext)",
    description="""Farm manager/owner only, and only for a **PROVISIONED** gateway — a
SUSPENDED/REJECTED/PENDING slot is a 409 with the current status named.

Stages a new password WITHOUT touching the active one: the old credential
keeps working at the broker until the device proves it received the new one
(`/rotation-ack/`). The plaintext is returned here exactly once (subsequent
config fetches see only the hash) and `rotation_pending` flips true.""",
    parameters=[
        OpenApiParameter(
            "gateway_slot_id", str, OpenApiParameter.PATH, description="Gateway slot id (caller's org)."
        )
    ],
    request=None,
    responses={
        200: CredentialRotationResponseSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(401, 403, 404),
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def rotate_credential(request, gateway_slot_id: str):
    """ROTATE_CREDENTIAL: stage a rotation and return the new plaintext
    password EXACTLY once. The old credential keeps working at the broker
    until the device confirms the new one."""
    org_id = _manager_or_error(request)
    if org_id is None:
        return Response({"error": "manager role required"}, status=403)
    slot = _owned_slot(gateway_slot_id, org_id)
    if slot is None:
        return Response({"error": "gateway not found in caller organization"}, status=404)
    if slot.provisioning_status != "PROVISIONED":
        return Response(
            {"error": f"rotation requires PROVISIONED gateway (status: {slot.provisioning_status})"},
            status=409,
        )

    from apps.mqtt_auth.services import stage_rotation

    raw = secrets.token_urlsafe(32)
    stage_rotation(slot.slot_id, raw)
    return Response({
        "gateway_slot_id": slot.slot_id,
        "username": slot.slot_id,
        "password": raw,  # one-time issue; subsequent fetches see only the hash
        "rotation_pending": True,
    }, status=200)


@extend_schema(
    tags=[TAG_PROVISIONING],
    operation_id="ack_gateway_rotation",
    summary="Commit a staged rotation (commit-on-ACK)",
    description="""Farm manager/owner only. Promotes the staged hash to active **only** when
the presented `new_password` matches it — that match is the proof the device
actually received the rotation, so a lost delivery can never lock a gateway
out of its broker.

No staged rotation, or a mismatch, is a 409 `no staged rotation or password
mismatch`; success returns `rotation_committed: true`.""",
    parameters=[
        OpenApiParameter(
            "gateway_slot_id", str, OpenApiParameter.PATH, description="Gateway slot id (caller's org)."
        )
    ],
    request=RotationAckRequestSerializer,
    responses={
        200: RotationAckResponseSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(401, 403, 404),
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def rotation_ack(request, gateway_slot_id: str):
    """Commit-on-ACK: promote the staged hash when the presented password
    matches it (proof the device received the rotated credential)."""
    org_id = _manager_or_error(request)
    if org_id is None:
        return Response({"error": "manager role required"}, status=403)
    slot = _owned_slot(gateway_slot_id, org_id)
    if slot is None:
        return Response({"error": "gateway not found in caller organization"}, status=404)

    from apps.mqtt_auth.services import commit_pending_rotation

    committed = commit_pending_rotation(
        slot.slot_id, str(request.data.get("new_password", ""))
    )
    if not committed:
        return Response({"error": "no staged rotation or password mismatch"}, status=409)
    return Response({"gateway_slot_id": slot.slot_id, "rotation_committed": True})
