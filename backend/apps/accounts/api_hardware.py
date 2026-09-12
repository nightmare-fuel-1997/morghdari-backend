"""Platform hardware registration — the Hardware Team write path (v0.9 H0-H2).

The Hardware Team actor had zero system presence: gateway/sensor/actuator
serials entered only via direct DB writes (only tests created registry
rows). These six endpoints (register + retire x three unit kinds) close that
hole behind the audited support session, following the M7-07 support-op
conventions exactly (staff-not-superuser + active session + incident anchor
via _support_op_context; SUPPORT_OP_* audit rows; 403-one-deny / 404-no-oracle).

One generic core serves all three kinds — the registries share a shape
(slot FK + hardware_id + org + installed/removed audit columns); only the
slot/hw models differ:

- gateway  -> slot: devices.GatewaySlot,          row: devices.GatewayHardwareRegistry
- sensor   -> slot: devices.SaloonSensorSlot,     row: devices.HardwareRegistry
- actuator -> slot: devices.SaloonActuatorSlot,   row: devices.ActuatorHardwareRegistry

Semantics (per kind):
- register: slot must be in the session org (else 404); blank ids/reason
  are 400; the identical active row re-registers idempotently (200 +
  idempotent:true, force-resolve precedent); a DIFFERENT live unit on the
  same slot is 409 (retire it first — install history must stay linear).
- retire: unknown/other-org row is 404; an already-retired row re-retires
  idempotently; otherwise stamps removed_at/removed_by.
"""

from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.serializers import (
    HardwareAclBackfillResponseSerializer,
    HardwareRegisterRequestSerializer,
    HardwareRegisterResponseSerializer,
    HardwareReplaceRequestSerializer,
    HardwareReplaceResponseSerializer,
    HardwareRetireRequestSerializer,
    HardwareRetireResponseSerializer,
)
from apps.accounts.views import _audit_support_op, _support_op_context
from apps.common.schemas import CONFLICT_RESPONSE, TAG_SUPPORT, error_responses
from apps.mqtt_auth.services import (
    hash_password,
    sync_gateway_acls,
    update_gateway_user_password,
)

_KINDS = ("gateway", "sensor", "actuator")


def _models(kind: str):
    from apps.devices.models import (
        ActuatorHardwareRegistry,
        GatewayHardwareRegistry,
        GatewaySlot,
        HardwareRegistry,
        SaloonActuatorSlot,
        SaloonSensorSlot,
    )

    return {
        "gateway": (GatewaySlot, GatewayHardwareRegistry),
        "sensor": (SaloonSensorSlot, HardwareRegistry),
        "actuator": (SaloonActuatorSlot, ActuatorHardwareRegistry),
    }[kind]


def _register(request, kind: str):
    org_id, incident_id, denied = _support_op_context(request)
    if denied is not None:
        return denied
    slot_id = str(request.data.get("slot_id") or "").strip()
    hardware_id = str(request.data.get("hardware_id") or "").strip()
    reason = str(request.data.get("reason") or "").strip()
    if not slot_id or not hardware_id:
        return Response({"error": "slot_id and hardware_id required"}, status=400)
    if not reason:
        return Response({"error": "reason required"}, status=400)

    slot_model, hw_model = _models(kind)
    slot = slot_model.objects.filter(pk=slot_id, org_id=org_id).first()
    if slot is None:
        return Response({"error": "slot not found in the session org"}, status=404)

    same = hw_model.objects.filter(
        slot=slot, hardware_id=hardware_id, org_id=org_id, removed_at__isnull=True
    ).first()
    if same is not None:
        return Response({"id": same.pk, "hardware_id": same.hardware_id, "slot_id": slot.pk, "idempotent": True})

    occupied = hw_model.objects.filter(slot=slot, org_id=org_id, removed_at__isnull=True).first()
    if occupied is not None:
        return Response(
            {"error": "slot already has live hardware (retire it first)"}, status=409
        )

    row = hw_model.objects.create(
        slot=slot, org_id=org_id, hardware_id=hardware_id, installed_by=request.user
    )
    _audit_support_op(
        request, org_id, "SUPPORT_OP_HW_REGISTER", incident_id,
        {"kind": kind, "hardware_id": hardware_id, "slot_id": slot.pk, "reason": reason},
    )
    return Response({"id": row.pk, "hardware_id": row.hardware_id, "slot_id": slot.pk})


def _retire(request, kind: str, hw_id: int):
    org_id, incident_id, denied = _support_op_context(request)
    if denied is not None:
        return denied
    reason = str(request.data.get("reason") or "").strip()
    if not reason:
        return Response({"error": "reason required"}, status=400)

    _, hw_model = _models(kind)
    row = hw_model.objects.filter(pk=hw_id, org_id=org_id).first()
    if row is None:
        return Response({"error": "hardware row not found in the session org"}, status=404)
    if row.removed_at is not None:
        return Response({"id": row.pk, "retired": True, "idempotent": True})

    row.removed_at = timezone.now()
    row.removed_by = request.user
    row.save(update_fields=["removed_at", "removed_by"])
    _audit_support_op(
        request, org_id, "SUPPORT_OP_HW_RETIRE", incident_id,
        {"kind": kind, "hardware_id": row.hardware_id, "reason": reason},
    )
    return Response({"id": row.pk, "retired": True})


def _register_schema(kind: str, summary: str, description: str):
    return extend_schema(
        tags=[TAG_SUPPORT],
        operation_id=f"hw_{kind}_register",
        summary=summary,
        description=description,
        request=HardwareRegisterRequestSerializer,
        responses={
            200: HardwareRegisterResponseSerializer,
            409: CONFLICT_RESPONSE,
            **error_responses(400, 401, 403, 404),
        },
        examples=[
            OpenApiExample(
                f"Register {kind} hardware",
                request_only=True,
                value={"slot_id": "HW-GW-01", "hardware_id": "GW-2026-0042", "reason": "factory batch 7, INC-2041"},
            )
        ],
    )


def _retire_schema(kind: str, summary: str, description: str):
    from drf_spectacular.utils import OpenApiParameter

    return extend_schema(
        tags=[TAG_SUPPORT],
        operation_id=f"hw_{kind}_retire",
        summary=summary,
        description=description,
        parameters=[
            OpenApiParameter("hw_id", int, OpenApiParameter.PATH, description="Registry row pk from the register response.")
        ],
        request=HardwareRetireRequestSerializer,
        responses={
            200: HardwareRetireResponseSerializer,
            **error_responses(400, 401, 403, 404),
        },
        examples=[
            OpenApiExample(
                f"Retire {kind} hardware",
                request_only=True,
                value={"reason": "dead radio, INC-2041"},
            )
        ],
    )


@_register_schema(
    "gateway",
    "Register a gateway hardware unit onto a slot",
    """Hardware Team write path (H0): creates the `gateway_hardware_registry`
row the installer sync requires (`active gateway hardware not registered`
otherwise). Idempotent on the identical active row; 409 when the slot
already carries a different live unit; foreign slot is 404.""",
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hw_gateway_register(request):
    """POST /platform/support/hardware/gateways/ — register a factory unit."""
    return _register(request, "gateway")


@_retire_schema(
    "gateway",
    "Retire a gateway hardware unit",
    """Closes the registry row (removed_at/removed_by); idempotent on an
already-retired row. Retiring the slot's last live unit blocks the installer
sync with an honest 400 until a new unit registers.""",
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hw_gateway_retire(request, hw_id: int):
    """POST /platform/support/hardware/gateways/{hw_id}/retire/."""
    return _retire(request, "gateway", hw_id)


@_register_schema(
    "sensor",
    "Register a sensor hardware unit onto a slot",
    """Hardware Team write path (H1): creates the `hardware_registry` row for
a sensor pack. Same register semantics as the gateway kind.""",
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hw_sensor_register(request):
    """POST /platform/support/hardware/sensors/ — register a factory unit."""
    return _register(request, "sensor")


@_retire_schema(
    "sensor",
    "Retire a sensor hardware unit",
    """Closes the sensor registry row; idempotent on an already-retired row.""",
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hw_sensor_retire(request, hw_id: int):
    """POST /platform/support/hardware/sensors/{hw_id}/retire/."""
    return _retire(request, "sensor", hw_id)


@_register_schema(
    "actuator",
    "Register an actuator hardware unit onto a slot",
    """Hardware Team write path (H2): creates the `actuator_hardware_registry`
row. Same register semantics as the gateway kind.""",
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hw_actuator_register(request):
    """POST /platform/support/hardware/actuators/ — register a factory unit."""
    return _register(request, "actuator")


@_retire_schema(
    "actuator",
    "Retire an actuator hardware unit",
    """Closes the actuator registry row; idempotent on an already-retired row.""",
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hw_actuator_retire(request, hw_id: int):
    """POST /platform/support/hardware/actuators/{hw_id}/retire/."""
    return _retire(request, "actuator", hw_id)


@extend_schema(
    tags=[TAG_SUPPORT],
    operation_id="hw_gateway_replace",
    summary="Swap the live gateway unit with fresh credentials (H3)",
    description="""Replacement ceremony in one atomic DB transaction: retire the
old unit, register the new serial, and set a FRESH MQTT password on the
slot's credential (the old password dies with the swap). The plaintext is
returned EXACTLY ONCE — a replayed ceremony answers idempotent:true with an
empty password and never re-issues. Broker push is FNV-soft
(broker_synced:false still completes the ceremony; rotation heals a missed
push). Writes SUPPORT_OP_HW_REPLACE — never the plaintext.""",
    request=HardwareReplaceRequestSerializer,
    responses={
        200: HardwareReplaceResponseSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Replace a dead unit",
            request_only=True,
            value={"new_hardware_id": "GW-2026-0043", "reason": "lightning strike, INC-2041"},
        )
    ],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hw_gateway_replace(request, hw_id: int):
    """POST /platform/support/hardware/gateways/{hw_id}/replace/ (H3)."""
    import secrets

    from django.db import transaction

    org_id, incident_id, denied = _support_op_context(request)
    if denied is not None:
        return denied
    new_hw = str(request.data.get("new_hardware_id") or "").strip()
    reason = str(request.data.get("reason") or "").strip()
    if not new_hw:
        return Response({"error": "new_hardware_id required"}, status=400)
    if not reason:
        return Response({"error": "reason required"}, status=400)

    from apps.devices.models import GatewayHardwareRegistry

    old = GatewayHardwareRegistry.objects.filter(
        pk=hw_id, org_id=org_id, removed_at__isnull=True
    ).first()
    if old is None:
        # Replay path: the ceremony already ran — the new unit is live.
        # Answer the stored state WITHOUT re-issuing (the one-time password
        # must never be emitted twice).
        live = GatewayHardwareRegistry.objects.filter(
            hardware_id=new_hw, org_id=org_id, removed_at__isnull=True
        ).first()
        if live is not None:
            return Response(
                {
                    "old_id": hw_id,
                    "new_id": live.pk,
                    "slot_id": live.slot_id,
                    "username": live.slot_id,
                    "password": "",
                    "broker_synced": False,
                    "idempotent": True,
                }
            )
        return Response({"error": "hardware row not active in the session org"}, status=404)
    if new_hw == old.hardware_id:
        return Response({"error": "new_hardware_id must differ from the live unit"}, status=400)
    clash = GatewayHardwareRegistry.objects.filter(
        hardware_id=new_hw, org_id=org_id, removed_at__isnull=True
    ).first()
    if clash is not None:
        return Response({"error": "new_hardware_id already live in this org"}, status=409)

    from apps.mqtt_auth.models import MqttDeviceCredential

    password = secrets.token_urlsafe(32)
    with transaction.atomic():
        old.removed_at = timezone.now()
        old.removed_by = request.user
        old.save(update_fields=["removed_at", "removed_by"])
        new_row = GatewayHardwareRegistry.objects.create(
            slot=old.slot, org_id=org_id, hardware_id=new_hw, installed_by=request.user
        )
        credential, _ = MqttDeviceCredential.objects.get_or_create(
            device_id=old.slot_id,
            defaults={
                "username": old.slot_id,
                "password_hash": hash_password(password),
            },
        )
        credential.username = old.slot_id
        credential.password_hash = hash_password(password)
        credential.pending_password_hash = None
        credential.is_active = True
        credential.last_rotated_at = timezone.now()
        credential.save()

    broker_ok = update_gateway_user_password(old.slot_id, password)
    acl = sync_gateway_acls(new_row.slot)
    broker_synced = bool(broker_ok) and bool(acl.get("reachable"))
    _audit_support_op(
        request, org_id, "SUPPORT_OP_HW_REPLACE", incident_id,
        {
            "old_id": old.pk,
            "new_id": new_row.pk,
            "slot_id": old.slot_id,
            "username": old.slot_id,
            "broker_synced": broker_synced,
            "reason": reason,
        },
    )
    return Response(
        {
            "old_id": old.pk,
            "new_id": new_row.pk,
            "slot_id": old.slot_id,
            "username": old.slot_id,
            "password": password,
            "broker_synced": broker_synced,
        }
    )


@extend_schema(
    tags=[TAG_SUPPORT],
    operation_id="hw_gateway_acl_backfill",
    summary="Backfill the full T1–T9 MQTT rule set onto a slot (H4)",
    description="""Slots approved before M8-04-H carry telemetry/status rules
only — under no_match=deny the real firmware's downlink (T3/T5/T6/T8) and
response publishes (T4/T7/T9) die at the broker. This creates the missing
allow-rows from the canonical `gateway_acl_rule_items` set (additive-only,
re-runs add zero) and re-pushes the broker sync. Writes
SUPPORT_OP_HW_ACL_BACKFILL.""",
    request=HardwareRetireRequestSerializer,
    responses={
        200: HardwareAclBackfillResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Backfill downlink rules",
            request_only=True,
            value={"reason": "pre-H4 slot, INC-2041"},
        )
    ],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hw_gateway_acl_backfill(request, slot_id: str):
    """POST /platform/support/hardware/gateways/{slot_id}/acl-backfill/ (H4)."""
    org_id, incident_id, denied = _support_op_context(request)
    if denied is not None:
        return denied
    reason = str(request.data.get("reason") or "").strip()
    if not reason:
        return Response({"error": "reason required"}, status=400)

    from apps.devices.models import GatewaySlot
    from apps.mqtt_auth.models import MqttAclRule
    from apps.provisioning.api import gateway_acl_rule_items

    slot = GatewaySlot.objects.filter(pk=slot_id, org_id=org_id).first()
    if slot is None:
        return Response({"error": "gateway not found in the session org"}, status=404)
    if slot.farm_id is None:
        return Response({"error": "slot has no farm; attach a farm first"}, status=400)

    added = 0
    for item in gateway_acl_rule_items(str(org_id), slot.farm_id, slot.slot_id):
        _, created = MqttAclRule.objects.get_or_create(
            username=slot.slot_id, action=item["action"], topic=item["topic"],
            defaults={"permission": "allow"},
        )
        added += bool(created)
    total = MqttAclRule.objects.filter(username=slot.slot_id).count()
    acl = sync_gateway_acls(slot)
    broker_synced = bool(acl.get("reachable"))
    _audit_support_op(
        request, org_id, "SUPPORT_OP_HW_ACL_BACKFILL", incident_id,
        {"slot_id": slot.slot_id, "added": added, "rules_total": total, "reason": reason},
    )
    return Response(
        {"slot_id": slot.slot_id, "rules_total": total, "added": added, "broker_synced": broker_synced}
    )
