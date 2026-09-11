"""Ops serializers — M6-10 (contract v1.7.0). Deliberately thin dict builders
like compat/serializers_compat.py: the O-keys are contractual, so they are
written once, explicitly, with the source column beside each — no ModelSerializer
magic that could silently rename a field.

The DRF Serializer classes below are DOCS-ONLY mirrors of those dict shapes
for drf-spectacular @extend_schema responses/requests. They are never used at
runtime (views keep calling the dict builders) so response bodies cannot drift.

فارسی: شکلِ کلیدها قراردادی است؛ پس دیکشنری‌ها آگاهانه و دستی ساخته می‌شوند.
"""

from rest_framework import serializers


def audit_item(row) -> dict:
    """O1 row — contract keys; created_at ← AccountActivityLog.timestamp."""
    return {
        "id": str(row.id),
        "actor_id": row.actor_id,
        "username": row.actor.username if row.actor_id else None,
        "action": row.action,
        "details": row.details,
        "created_at": row.timestamp.isoformat().replace("+00:00", "Z") if row.timestamp else None,
    }


def threshold_item(rule) -> dict:
    """O2/O3 row — the real LifecycleThresholdRule schema (day-ranged bands).
    The draft spec's `enabled`/`updated_at` keys have NO columns in the frozen
    table — the ratifying yaml (and this row) matches the real schema instead
    (erratum noted in the spec file)."""
    return {
        "id": rule.id,
        "metric_key": rule.metric,
        "template": rule.template_id,
        "day_from": rule.day_from,
        "day_to": rule.day_to,
        "min": float(rule.min_value) if rule.min_value is not None else None,
        "max": float(rule.max_value) if rule.max_value is not None else None,
    }


def template_item(t) -> dict:
    """O4 row — the read-only lifecycle_templates catalog."""
    return {
        "template_id": t.template_id,
        "org_id": t.org_id,
        "breed_type": t.breed_type,
        "name": t.name,
        "is_default": bool(t.is_default),
    }


def photo_item(p) -> dict:
    """O5 row — contract keys; url ← file_url, taken_at ← uploaded_at."""
    return {
        "id": p.id,
        "url": p.file_url,
        "caption": p.description,
        "taken_at": p.uploaded_at.isoformat().replace("+00:00", "Z") if p.uploaded_at else None,
        "uploaded_by": p.uploaded_by_id,
    }


def gateway_item(slot, saloon_count: int, last_seen_at) -> dict:
    """O7 row — last_seen_at arrives from the diag register (O9's store)."""
    return {
        "slot_id": slot.slot_id,
        "farm_id": slot.farm_id,
        "provisioning_status": slot.provisioning_status,
        "last_seen_at": last_seen_at,
        "saloon_count": saloon_count,
    }


# ---------------------------------------------------------------------------
# Docs-only DRF mirrors for @extend_schema (never used at runtime).
# Each mirrors the dict builder above + backend/openapi.yaml examples.
# ---------------------------------------------------------------------------


class AuditLogItemSerializer(serializers.Serializer):
    """O1 row — created_at ← AccountActivityLog.timestamp (ISO Z)."""

    id = serializers.CharField(help_text="Row id (string).")
    actor_id = serializers.IntegerField(
        allow_null=True, required=False, help_text="Actor user id; null for system rows."
    )
    username = serializers.CharField(
        allow_null=True, required=False, help_text="Actor username; null when no actor."
    )
    action = serializers.CharField(help_text="e.g. THRESHOLD_UPDATED.")
    details = serializers.DictField(
        required=False, allow_null=True, help_text="Action payload (free-form object)."
    )
    created_at = serializers.CharField(
        allow_null=True, required=False, help_text="ISO Z timestamp."
    )


class AuditLogListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(help_text="Total rows matching the query.")
    results = AuditLogItemSerializer(many=True)


class ThresholdItemSerializer(serializers.Serializer):
    """O2/O3 row — day-ranged band; min/max are the contract keys."""

    id = serializers.IntegerField()
    metric_key = serializers.CharField(help_text="Source column: metric.")
    template = serializers.IntegerField(
        allow_null=True, required=False, help_text="Source column: template_id."
    )
    day_from = serializers.IntegerField()
    day_to = serializers.IntegerField()
    min = serializers.FloatField(allow_null=True)
    max = serializers.FloatField(allow_null=True)


class ThresholdListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = ThresholdItemSerializer(many=True)


class ThresholdUpdateRequestSerializer(serializers.Serializer):
    """O3 PUT body — contract {min, max} keys (partial PUT allowed)."""

    min = serializers.FloatField(
        required=False, allow_null=True, help_text="New min_value (alias of min_value)."
    )
    max = serializers.FloatField(
        required=False, allow_null=True, help_text="New max_value (alias of max_value)."
    )


class ThresholdTemplateItemSerializer(serializers.Serializer):
    """O4 row — read-only lifecycle_templates catalog."""

    template_id = serializers.IntegerField()
    org_id = serializers.IntegerField(allow_null=True, required=False)
    breed_type = serializers.CharField(allow_null=True, required=False)
    name = serializers.CharField(allow_null=True, required=False)
    is_default = serializers.BooleanField(required=False)


class ThresholdTemplateListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = ThresholdTemplateItemSerializer(many=True)


class PhotoItemSerializer(serializers.Serializer):
    """O5/O6 row — url ← file_url, taken_at ← uploaded_at (ISO Z)."""

    id = serializers.IntegerField()
    url = serializers.CharField(help_text="Serviceable file URL.")
    caption = serializers.CharField(allow_null=True, required=False)
    taken_at = serializers.CharField(allow_null=True, required=False)
    uploaded_by = serializers.IntegerField(allow_null=True, required=False)


class PhotoListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = PhotoItemSerializer(many=True)


class PhotoUploadRequestSerializer(serializers.Serializer):
    """O6 multipart body — jpg/png ≤10 MB (spec §2 O6)."""

    file = serializers.FileField(help_text="jpg/png, max 10 MB.")
    caption = serializers.CharField(
        required=False, allow_blank=True, help_text="Stored as description (≤255)."
    )
    taken_at = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="ISO Z; defaults to now when omitted/invalid.",
    )


class GatewayItemSerializer(serializers.Serializer):
    """O7 row — last_seen_at comes from the diag register (O9's store)."""

    slot_id = serializers.CharField()
    farm_id = serializers.IntegerField(allow_null=True, required=False)
    provisioning_status = serializers.CharField()
    last_seen_at = serializers.CharField(allow_null=True, required=False)
    saloon_count = serializers.IntegerField()


class GatewayListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = GatewayItemSerializer(many=True)


class GatewaySaloonZoneSerializer(serializers.Serializer):
    saloon_id = serializers.IntegerField()
    saloon_name = serializers.CharField(allow_null=True, required=False)
    assigned_at = serializers.CharField(allow_null=True, required=False)


class GatewayDetailResponseSerializer(serializers.Serializer):
    """O8 — gateway + saloon zones + active hardware."""

    slot_id = serializers.CharField()
    farm_id = serializers.IntegerField(allow_null=True, required=False)
    provisioning_status = serializers.CharField()
    last_seen_at = serializers.CharField(allow_null=True, required=False)
    saloon_count = serializers.IntegerField()
    saloon_zones = GatewaySaloonZoneSerializer(many=True)
    active_hardware_id = serializers.CharField(allow_null=True, required=False)


class GatewayDiagnosticsSerializer(serializers.Serializer):
    """O9 — the retained diag:last:{slot} hash (honest nulls, never fabricated)."""

    slot_id = serializers.CharField()
    reported_at = serializers.CharField(allow_null=True, required=False)
    battery_v = serializers.FloatField(allow_null=True, required=False)
    rssi = serializers.FloatField(allow_null=True, required=False)
    free_heap = serializers.FloatField(allow_null=True, required=False)
    sd_ok = serializers.BooleanField(allow_null=True, required=False)
    errors = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="Device error strings."
    )


class PasswordChangeRequestSerializer(serializers.Serializer):
    current_password = serializers.CharField(
        write_only=True, help_text="Current password (401 when wrong)."
    )
    new_password = serializers.CharField(
        write_only=True,
        min_length=10,
        help_text="≥10 chars, denylist + Django validators, must differ from current.",
    )


class PasswordChangeResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text='Always "ok" on success.')


class SessionItemSerializer(serializers.Serializer):
    """O11 row — the caller's own live session register."""

    id = serializers.IntegerField()
    created_at = serializers.CharField(allow_null=True, required=False)
    last_seen_at = serializers.CharField(allow_null=True, required=False)
    ip = serializers.CharField(allow_null=True, required=False)
    user_agent = serializers.CharField(allow_null=True, required=False)
    current = serializers.BooleanField(help_text="True for the presenting session.")


class SessionRevokeResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text='Always "revoked" on success.')
    id = serializers.IntegerField()
