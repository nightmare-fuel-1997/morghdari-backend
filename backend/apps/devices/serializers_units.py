"""Docs-only serializers for the factory inventory endpoints (Phase 1).

Same posture as `apps/provisioning/serializers.py`: these describe what the
views actually return so the generated OpenAPI matches, and they validate the
request bodies. No model, no migration.
"""

from rest_framework import serializers


class UnitRegisterRequestSerializer(serializers.Serializer):
    """POST /platform/devices/units/ — mint one factory unit.

    `unit_id` is optional: when omitted the backend generates `GW-<16 hex>`. It
    becomes the permanent logical identity (AD-2) — the MQTT username, and the
    eventual `gateway_slots.slot_id` — so an operator who supplies it must not
    later expect to change it.
    """

    hardware_id = serializers.CharField(
        max_length=100, help_text="Factory serial / MAC printed on the unit. Unique."
    )
    unit_id = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
        help_text="Permanent logical id. Omit to generate `GW-<hex>`.",
    )
    model = serializers.CharField(max_length=50, required=False, allow_blank=True)
    fw_version = serializers.CharField(max_length=50, required=False, allow_blank=True)
    batch = serializers.CharField(max_length=50, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    reason = serializers.CharField(
        max_length=255, required=False, allow_blank=True,
        help_text="Optional justification, recorded in platform_audit_log.",
    )


class UnitReasonSerializer(serializers.Serializer):
    """Body for claim-code re-issue and credential rotation."""

    reason = serializers.CharField(
        max_length=255, required=False, allow_blank=True,
        help_text="Optional justification, recorded in platform_audit_log.",
    )


class UnitRetireRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(
        max_length=255, help_text="MANDATORY justification — a blank value is a 400."
    )


class UnitRegisterResponseSerializer(serializers.Serializer):
    """The two secrets are returned EXACTLY ONCE, here and nowhere else.

    `password` goes into the unit's encrypted flash; `claim_code` goes onto the
    QR label. Neither is ever readable again — a lost claim code is re-issued
    (which rotates it), and a lost password is rotated.
    """

    unit_id = serializers.CharField()
    hardware_id = serializers.CharField()
    username = serializers.CharField(help_text="Always equal to unit_id.")
    password = serializers.CharField(help_text="One-time MQTT credential — write to flash, then forget.")
    claim_code = serializers.CharField(help_text="One-time QR claim code — print on the label.")
    state = serializers.CharField()
    acl_rules = serializers.IntegerField(help_text="Allow-rows created (always 3 for an unbound unit).")


class UnitCredentialResponseSerializer(serializers.Serializer):
    unit_id = serializers.CharField()
    username = serializers.CharField()
    password = serializers.CharField(help_text="One-time plaintext; the stored value is a bcrypt hash.")


class UnitClaimCodeResponseSerializer(serializers.Serializer):
    unit_id = serializers.CharField()
    claim_code = serializers.CharField(help_text="One-time plaintext; the stored value is a bcrypt hash.")


class UnitRetireResponseSerializer(serializers.Serializer):
    unit_id = serializers.CharField()
    retired = serializers.BooleanField()
    idempotent = serializers.BooleanField(required=False)


class UnitItemSerializer(serializers.Serializer):
    unit_id = serializers.CharField()
    hardware_id = serializers.CharField()
    state = serializers.CharField()
    model = serializers.CharField(allow_null=True, required=False)
    fw_version = serializers.CharField(allow_null=True, required=False)
    batch = serializers.CharField(allow_null=True, required=False)
    slot_id = serializers.CharField(allow_null=True, required=False)
    created_at = serializers.CharField(allow_null=True, required=False)
    bound_at = serializers.CharField(allow_null=True, required=False)
    retired_at = serializers.CharField(allow_null=True, required=False)
    credential_active = serializers.BooleanField(
        help_text="False once the unit is retired or its credential is deactivated."
    )


class UnitListResponseSerializer(serializers.Serializer):
    results = UnitItemSerializer(many=True)
    count = serializers.IntegerField()
