"""Factory inventory endpoints — the Hardware Team's panel (Phase 1).

WHY these live outside `/platform/support/`: the Hardware Team works BEFORE any
organization exists, and every `/platform/support/*` endpoint requires an
org-scoped support session (`accounts/views.py::_support_op_context`). Reusing
that gate would make factory registration impossible (design doc §5.2, corpus
contradiction X1). The gate here is `is_platform_staff` + `staff_role`
(`apps/accounts/platform_gate.py`).

What one register call mints (and why it is one call): a `device_units` row, its
`mqtt_device_credentials` row, its single-use `claim_code`, and exactly three
ACL rows. Splitting these would allow a unit to exist with a credential but no
ACL (an invisible failure at the broker) or with an ACL but no credential.

Secrets discipline: the MQTT password and the claim code are returned EXACTLY
ONCE, in the register (or re-issue) response. Neither is retrievable afterwards —
a lost claim code is re-issued (which rotates it) and a lost password is rotated.
Nothing here ever logs either value; the audit rows carry counts and reasons only.
"""

from __future__ import annotations

import secrets

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.platform_gate import (
    HARDWARE_STAFF_ROLES,
    INVENTORY_READ_ROLES,
    staff_denied,
)
from apps.auditing.models import PlatformAuditLog
from apps.devices.models import DeviceUnit
from apps.devices.serializers_units import (
    UnitClaimCodeResponseSerializer,
    UnitCredentialResponseSerializer,
    UnitListResponseSerializer,
    UnitReasonSerializer,
    UnitRegisterRequestSerializer,
    UnitRegisterResponseSerializer,
    UnitRetireRequestSerializer,
    UnitRetireResponseSerializer,
)
from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
from apps.mqtt_auth.services import (
    bootstrap_acl_rule_items,
    ensure_gateway_user,
    hash_password,
)

TAG = "platform-inventory"
LIST_LIMIT = 200


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _audit(request, action: str, unit_id: str, details: dict) -> None:
    """Org-less audit row — see PlatformAuditLog's docstring for why one is needed."""
    PlatformAuditLog.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        action=action,
        target_type="device_unit",
        target_id=unit_id,
        ip_address=request.META.get("REMOTE_ADDR"),
        details=details,
    )


def _write_bootstrap_acls(unit_id: str) -> int:
    """Create the 3 device-lane allow-rows. Idempotent (get_or_create)."""
    written = 0
    for item in bootstrap_acl_rule_items(unit_id):
        _, created = MqttAclRule.objects.get_or_create(
            username=unit_id,
            topic=item["topic"],
            defaults={"permission": item["permission"], "action": item["action"]},
        )
        written += bool(created)
    return written


def _unit_out(unit: DeviceUnit, credential_active: bool) -> dict:
    return {
        "unit_id": unit.unit_id,
        "hardware_id": unit.hardware_id,
        "state": unit.state,
        "model": unit.model,
        "fw_version": unit.fw_version,
        "batch": unit.batch,
        "slot_id": unit.slot_id,
        "created_at": unit.created_at.isoformat() if unit.created_at else None,
        "bound_at": unit.bound_at.isoformat() if unit.bound_at else None,
        "retired_at": unit.retired_at.isoformat() if unit.retired_at else None,
        "credential_active": credential_active,
    }


def _not_found():
    # One verdict for "unknown" and "retired": the endpoint must not become an
    # inventory oracle for someone holding only a guessed id.
    return Response({"error": "unit not found"}, status=404)


def _reason(request) -> str:
    return str(request.data.get("reason") or "").strip()


# ---------------------------------------------------------------------------
# POST /platform/devices/units/
# ---------------------------------------------------------------------------

def _register(request):
    denied = staff_denied(request, HARDWARE_STAFF_ROLES)
    if denied is not None:
        return denied

    serializer = UnitRegisterRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({"error": "invalid body", "detail": serializer.errors}, status=400)
    data = serializer.validated_data

    hardware_id = (data.get("hardware_id") or "").strip()
    if not hardware_id:
        return Response({"error": "hardware_id required"}, status=400)

    # AD-2: unit_id is the permanent logical identity — the MQTT username and the
    # eventual gateway_slots.slot_id. Generated when the operator does not supply
    # one; never reusable, never renameable afterwards.
    unit_id = (data.get("unit_id") or "").strip() or f"GW-{secrets.token_hex(8).upper()}"

    password = secrets.token_urlsafe(32)
    claim_code = secrets.token_urlsafe(16)

    with transaction.atomic():
        if DeviceUnit.objects.filter(pk=unit_id).exists():
            return Response({"error": "unit_id already exists"}, status=409)
        if DeviceUnit.objects.filter(hardware_id=hardware_id).exists():
            return Response({"error": "hardware_id already registered"}, status=409)

        unit = DeviceUnit.objects.create(
            unit_id=unit_id,
            hardware_id=hardware_id,
            claim_code_hash=hash_password(claim_code),
            model=(data.get("model") or "").strip() or None,
            fw_version=(data.get("fw_version") or "").strip() or None,
            batch=(data.get("batch") or "").strip() or None,
            notes=(data.get("notes") or "").strip() or None,
            state="REGISTERED",
            created_by=request.user,
        )
        MqttDeviceCredential.objects.create(
            device_id=unit_id,
            username=unit_id,
            password_hash=hash_password(password),
            is_active=True,
            last_rotated_at=timezone.now(),
        )
        acl_count = _write_bootstrap_acls(unit_id)
        _audit(
            request, "PLATFORM_UNIT_REGISTER", unit_id,
            {
                "hardware_id": hardware_id,
                "batch": unit.batch,
                "model": unit.model,
                "acl_rules": acl_count,
                "reason": _reason(request),
            },
        )

    # Broker-side user push is FNV-soft and deliberately OUTSIDE the
    # transaction: production authn is postgres-only, so this is usually a
    # no-op, and a broker outage must never roll back a factory record.
    ensure_gateway_user(unit_id, password)

    return Response(
        {
            "unit_id": unit_id,
            "hardware_id": hardware_id,
            "username": unit_id,
            "password": password,
            "claim_code": claim_code,
            "state": unit.state,
            "acl_rules": acl_count,
        },
        status=201,
    )


# ---------------------------------------------------------------------------
# POST /platform/devices/units/{unit_id}/credential/
# ---------------------------------------------------------------------------

def _credential(request, unit_id: str):
    denied = staff_denied(request, HARDWARE_STAFF_ROLES)
    if denied is not None:
        return denied

    password = secrets.token_urlsafe(32)
    with transaction.atomic():
        unit = DeviceUnit.objects.select_for_update().filter(pk=unit_id).first()
        if unit is None or unit.state == "RETIRED":
            return _not_found()

        MqttDeviceCredential.objects.update_or_create(
            device_id=unit_id,
            defaults={
                "username": unit_id,
                "password_hash": hash_password(password),
                "is_active": True,
                "pending_password_hash": None,
                "pending_since": None,
                "last_rotated_at": timezone.now(),
            },
        )
        _audit(request, "PLATFORM_UNIT_CREDENTIAL", unit_id, {"reason": _reason(request)})

    ensure_gateway_user(unit_id, password)
    return Response({"unit_id": unit_id, "username": unit_id, "password": password})


# ---------------------------------------------------------------------------
# POST /platform/devices/units/{unit_id}/claim-code/
# ---------------------------------------------------------------------------

def _claim_code(request, unit_id: str):
    denied = staff_denied(request, HARDWARE_STAFF_ROLES)
    if denied is not None:
        return denied

    claim_code = secrets.token_urlsafe(16)
    with transaction.atomic():
        unit = DeviceUnit.objects.select_for_update().filter(pk=unit_id).first()
        if unit is None or unit.state == "RETIRED":
            return _not_found()
        if unit.state == "BOUND":
            return Response({"error": "unit is bound; the binding is the record"}, status=409)

        unit.claim_code_hash = hash_password(claim_code)
        unit.save(update_fields=["claim_code_hash"])
        _audit(request, "PLATFORM_UNIT_CLAIM_CODE", unit_id, {"reason": _reason(request)})

    return Response({"unit_id": unit_id, "claim_code": claim_code})


# ---------------------------------------------------------------------------
# POST /platform/devices/units/{unit_id}/retire/
# ---------------------------------------------------------------------------

def _retire(request, unit_id: str):
    denied = staff_denied(request, HARDWARE_STAFF_ROLES)
    if denied is not None:
        return denied

    reason = _reason(request)
    if not reason:
        return Response({"error": "reason required"}, status=400)

    with transaction.atomic():
        unit = DeviceUnit.objects.select_for_update().filter(pk=unit_id).first()
        if unit is None:
            return _not_found()
        if unit.state == "RETIRED":
            return Response({"unit_id": unit_id, "retired": True, "idempotent": True})
        if unit.state == "BOUND":
            return Response(
                {"error": "unit is bound to a gateway slot; unbind it first"}, status=409
            )

        unit.state = "RETIRED"
        unit.retired_at = timezone.now()
        unit.save(update_fields=["state", "retired_at"])

        MqttDeviceCredential.objects.filter(device_id=unit_id).update(is_active=False)
        deleted, _ = MqttAclRule.objects.filter(username=unit_id).delete()
        _audit(
            request, "PLATFORM_UNIT_RETIRE", unit_id,
            {"reason": reason, "acl_rows_deleted": deleted},
        )

    return Response({"unit_id": unit_id, "retired": True})


# ---------------------------------------------------------------------------
# GET /platform/devices/units/
# ---------------------------------------------------------------------------

def _list(request):
    denied = staff_denied(request, INVENTORY_READ_ROLES)
    if denied is not None:
        return denied

    queryset = DeviceUnit.objects.all().order_by("-created_at")
    state = str(request.query_params.get("state") or "").strip().upper()
    if state:
        if state not in {"REGISTERED", "BOUND", "RETIRED"}:
            return Response({"error": "invalid state filter"}, status=400)
        queryset = queryset.filter(state=state)

    units = list(queryset[:LIST_LIMIT])
    active = {
        row["device_id"]: row["is_active"]
        for row in MqttDeviceCredential.objects.filter(
            device_id__in=[u.unit_id for u in units]
        ).values("device_id", "is_active")
    }
    return Response(
        {
            "results": [_unit_out(u, bool(active.get(u.unit_id, False))) for u in units],
            "count": len(units),
        }
    )


# ---------------------------------------------------------------------------
# URL entry points
# ---------------------------------------------------------------------------

@extend_schema(
    tags=[TAG],
    operation_id="platform_unit_register",
    summary="Register a factory unit (Hardware Team)",
    description="""Mints one `device_units` row with its broker credential, its
single-use QR claim code, and exactly three ACL rows (subscribe `desired`,
subscribe `command`, publish `reported`).

The unit has NO organization and NO farm — that is the point (AD-1). It may
connect to the broker immediately, but its ACL reaches no tenant topic, so it
cannot publish telemetry until an installer binds it (Phase 2).

`password` and `claim_code` are returned exactly once and never logged.""",
    request=UnitRegisterRequestSerializer,
    responses={201: UnitRegisterResponseSerializer},
    methods=["POST"],
)
@extend_schema(
    tags=[TAG],
    operation_id="platform_unit_list",
    summary="List factory units (Hardware Team + Support)",
    description="""Newest first, capped at 200. `?state=` filters to
REGISTERED / BOUND / RETIRED.

Readable by support as well as hardware: support needs to answer "is this serial
already claimed?" without write access to the inventory.""",
    parameters=[
        OpenApiParameter(
            "state", str, OpenApiParameter.QUERY, required=False,
            description="REGISTERED | BOUND | RETIRED",
        )
    ],
    responses={200: UnitListResponseSerializer},
    methods=["GET"],
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def unit_collection(request):
    """GET → inventory, POST → register. The role gate lives in each impl."""
    if request.method == "POST":
        return _register(request)
    return _list(request)


@extend_schema(
    tags=[TAG],
    operation_id="platform_unit_credential",
    summary="Issue or rotate a unit's MQTT password (Hardware Team)",
    description="""Sets a fresh broker password and returns the plaintext once.

A retired unit is 404 (it must be re-registered, not resurrected). The previous
password stops working at the next CONNECT: the broker reads the hash straight
from `mqtt_device_credentials`.""",
    request=UnitReasonSerializer,
    responses={200: UnitCredentialResponseSerializer},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def unit_credential(request, unit_id: str):
    return _credential(request, unit_id)


@extend_schema(
    tags=[TAG],
    operation_id="platform_unit_claim_code",
    summary="Re-issue a unit's QR claim code (Hardware Team)",
    description="""Rotates the claim code and returns the new plaintext once.

Use when a label is damaged or the unit came back from the field. The previous
code stops working immediately — it is replaced, not supplemented. A BOUND unit
is a 409: its label is already consumed and the binding is the record.""",
    request=UnitReasonSerializer,
    responses={200: UnitClaimCodeResponseSerializer},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def unit_claim_code(request, unit_id: str):
    return _claim_code(request, unit_id)


@extend_schema(
    tags=[TAG],
    operation_id="platform_unit_retire",
    summary="Retire a factory unit (Hardware Team)",
    description="""Terminal: the unit stops authenticating (credential deactivated)
and loses every ACL row, so `no_match = deny` applies.

A BOUND unit is a 409 — unbind it first, because retiring it would leave a
gateway slot pointing at a unit that can no longer connect. Idempotent on an
already-retired unit.""",
    request=UnitRetireRequestSerializer,
    responses={200: UnitRetireResponseSerializer},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def unit_retire(request, unit_id: str):
    return _retire(request, unit_id)
