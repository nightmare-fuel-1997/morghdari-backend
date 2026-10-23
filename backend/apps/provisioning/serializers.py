"""provisioning OpenAPI components — docs-only serializer set (contract v1.7.0).

No model, no view, no migration. Every shape mirrors what apps/provisioning/api.py
actually returns (`_geo_out_*`, `_request_out`, `gateway_config_payload`, the
lifecycle/rotation envelopes) so the generated schema matches
backend/openapi.yaml instead of inventing shapes.

Two things are encoded deliberately:

* the geography reads are GLOBAL reference data (no org scope) while the farm
  leaves of the location tree are org-scoped — the tree therefore carries farm
  ids, not farm objects.
* the one-time plaintext gateway password appears ONLY in the approval and
  rotation responses; every later config fetch sees `password_hash` alone
  (that asymmetry is the M5-06/M5-08 contract, not an oversight).

فارسی: طرح‌واره‌های مستندسازی راه‌اندازی گیت‌وی — فقط برای OpenAPI، بدون اثر
اجرایی؛ رمزِ یک‌بارمصرف فقط در پاسخ تأیید/چرخش برمی‌گردد.
"""

from rest_framework import serializers

PROVISIONING_STATUSES = [
    "PENDING_INSTALLATION",
    "PENDING_SYNC",
    "PENDING_APPROVAL",
    "PROVISIONED",
    "SUSPENDED",
    "REJECTED",
]


# ---------------------------------------------------------------------------
# M6-01 geography reads (global reference data)
# ---------------------------------------------------------------------------


class GeoProvinceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class GeoCitySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    province_id = serializers.IntegerField()
    name = serializers.CharField()


class GeoVillageSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    city_id = serializers.IntegerField()
    name = serializers.CharField()


class SaloonPositionSerializer(serializers.Serializer):
    """One sensor position of a saloon — installer-facing reference rows."""

    id = serializers.IntegerField()
    saloon_id = serializers.IntegerField()
    label = serializers.CharField()


class LocationTreeFarmSerializer(serializers.Serializer):
    """The only org-scoped node of the tree (farm leaves)."""

    farm_id = serializers.IntegerField()
    name = serializers.CharField()


class LocationTreeVillageSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class LocationTreeCitySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    villages = LocationTreeVillageSerializer(many=True)
    farms = LocationTreeFarmSerializer(
        many=True, help_text="The caller org's farms in this city only."
    )


class LocationTreeProvinceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    cities = LocationTreeCitySerializer(many=True)


# ---------------------------------------------------------------------------
# Gateway requests + review (M5-02/03)
# ---------------------------------------------------------------------------


class GatewayRequestSerializer(serializers.Serializer):
    """GatewayRequestOut — one row of GET/POST /gateway-requests/."""

    id = serializers.IntegerField()
    org_id = serializers.CharField(help_text="Stringified org UUID.")
    farm_id = serializers.IntegerField(allow_null=True, required=False)
    status = serializers.ChoiceField(choices=["PENDING", "APPROVED", "REJECTED"])
    requested_by = serializers.IntegerField(allow_null=True, required=False)
    requested_at = serializers.CharField(allow_null=True, required=False, help_text="ISO Z.")
    approved_by = serializers.IntegerField(allow_null=True, required=False)
    approved_at = serializers.CharField(allow_null=True, required=False, help_text="ISO Z.")
    rejection_reason = serializers.CharField(allow_null=True, required=False)
    details = serializers.DictField(
        allow_null=True, required=False, help_text="Free-form installer/topology context."
    )


class GatewayRequestCreateSerializer(serializers.Serializer):
    """POST /gateway-requests/ body — manager/owner only, capped per org.

    AD-15 (owner 2026-10-18): the request may CREATE the farm, because the org
    rep enters "the farm's initial information" before the farm exists. Send
    `farm_spec` for a new farm, or `farm_id` for one that already exists —
    exactly one of the two, never both and never neither.

    AD-16: `expected_gateways` may be > 1 (a gateway owns a zone of saloons, not
    the whole farm — Saas.md). Approval materialises one installation job per
    gateway.
    """

    farm_id = serializers.IntegerField(
        required=False, allow_null=True, help_text="Existing farm the gateway will serve."
    )
    farm_spec = serializers.DictField(
        required=False,
        allow_null=True,
        help_text="New-farm spec: {name, province_id, city_id, village_id?, notes?}.",
    )
    expected_gateways = serializers.IntegerField(
        required=False, min_value=1, max_value=32, default=1,
        help_text="How many gateways this farm needs (AD-16).",
    )
    details = serializers.DictField(
        required=False,
        allow_null=True,
        help_text="Free-form context (contact, install window, notes).",
    )

    def validate(self, attrs):
        has_farm = attrs.get("farm_id") is not None
        has_spec = attrs.get("farm_spec") is not None
        if has_farm and has_spec:
            raise serializers.ValidationError(
                {"farm_spec": "send farm_id OR farm_spec, not both"}
            )
        if not has_farm and not has_spec:
            raise serializers.ValidationError(
                {"farm_id": "farm_id or farm_spec is required"}
            )
        if has_spec:
            spec = attrs["farm_spec"]
            missing = [
                key
                for key in ("name", "province_id", "city_id")
                if spec.get(key) in (None, "")
            ]
            if missing:
                raise serializers.ValidationError(
                    {"farm_spec": f"missing required keys: {', '.join(missing)}"}
                )
        return attrs


class GatewayReviewRequestSerializer(serializers.Serializer):
    """POST /gateway-requests/{id}/review/ body — platform review permission."""

    decision = serializers.ChoiceField(
        choices=["APPROVE", "REJECT"], help_text="Case-insensitive; anything else is a 400."
    )
    reason = serializers.CharField(
        required=False, allow_blank=True, help_text="Rejection reason (defaults to 'rejected')."
    )
    slot_id = serializers.CharField(
        required=False,
        help_text="Optional slot id to mint; generated (`GW-…`) when omitted.",
    )


class GatewayReviewApprovedSerializer(GatewayRequestSerializer):
    """Approval response — the request plus the minted gateway identity.

    `password` is the one-time plaintext (the broker credential is stored as a
    bcrypt hash); later fetches only ever see the hash.
    """

    gateway_slot_id = serializers.CharField()
    username = serializers.CharField()
    password = serializers.CharField(
        help_text="One-time plaintext MQTT credential — shown exactly once."
    )


# ---------------------------------------------------------------------------
# Installer sync (M5-03/04/09)
# ---------------------------------------------------------------------------


class GatewayZoneAssignmentSerializer(serializers.Serializer):
    """One saloon the gateway serves (create-if-absent, idempotent)."""

    saloon_id = serializers.IntegerField()


class GatewaySensorMappingSerializer(serializers.Serializer):
    """Pack → sensor-slot mapping written by the installer."""

    pack_id = serializers.CharField()
    slot_id = serializers.CharField(help_text="SaloonSensorSlot id, active, caller's org.")


class GatewayReplacementSerializer(serializers.Serializer):
    """M5-03 hardware swap: retire the old registry row, re-point the pack."""

    old_hardware_id = serializers.CharField()
    new_hardware_id = serializers.CharField()
    pack_id = serializers.CharField()
    slot_id = serializers.CharField(help_text="Sensor slot the pack is re-pointed to.")


class ProvisioningSyncRequestSerializer(serializers.Serializer):
    """POST /provisioning/sync/ body — installer role only.

    Every list is validated UP FRONT (before any write) so a malformed entry can
    never leave half-written topology behind (M5-09)."""

    gateway_slot_id = serializers.CharField()
    zones = GatewayZoneAssignmentSerializer(
        many=True, required=False, help_text="Saloon ids to assign (accepts ints directly)."
    )
    sensor_slot_mappings = GatewaySensorMappingSerializer(many=True, required=False)
    replacements = GatewayReplacementSerializer(many=True, required=False)


class ProvisioningSyncResponseSerializer(serializers.Serializer):
    """Sync result — the slot moved to PENDING_APPROVAL."""

    gateway_slot_id = serializers.CharField()
    gateway_hardware_id = serializers.CharField(help_text="Active registry row used for the sync.")
    status = serializers.CharField(help_text="Always PENDING_APPROVAL after a sync.")
    zones_count = serializers.IntegerField()
    mappings_count = serializers.IntegerField()
    replacements_count = serializers.IntegerField()


# ---------------------------------------------------------------------------
# M5-05 lifecycle actions + M5-06 config + M5-08 rotation
# ---------------------------------------------------------------------------


class GatewayStatusResponseSerializer(serializers.Serializer):
    """Approved/rejected/suspended/resumed — the resulting provisioning status."""

    gateway_slot_id = serializers.CharField()
    status = serializers.ChoiceField(choices=PROVISIONING_STATUSES)


class GatewayActionRequestSerializer(serializers.Serializer):
    """Body of reject/suspend — the reason is optional free text."""

    reason = serializers.CharField(required=False, allow_blank=True)


class GatewayMqttSettingsSerializer(serializers.Serializer):
    host = serializers.CharField()
    port = serializers.IntegerField()
    tls = serializers.BooleanField(help_text="True when the deployment requires TLS.")


class GatewayConfigAuthSerializer(serializers.Serializer):
    username = serializers.CharField(allow_null=True, required=False)
    password_hash = serializers.CharField(
        allow_null=True, required=False, help_text="bcrypt hash — never the plaintext."
    )
    password_issued_once = serializers.BooleanField(
        help_text="True: the plaintext was returned exactly once (approval/rotation)."
    )
    rotation_pending = serializers.BooleanField(
        help_text="True while a staged (unconfirmed) rotation is waiting for the device ACK."
    )


class GatewayConfigResponseSerializer(serializers.Serializer):
    """R2 contract-safe config — deliberately no sensor slot ids in the payload."""

    gateway_slot_id = serializers.CharField()
    org_id = serializers.CharField(help_text="Stringified org UUID.")
    farm_id = serializers.IntegerField(allow_null=True, required=False)
    provisioning_status = serializers.ChoiceField(choices=PROVISIONING_STATUSES)
    mqtt = GatewayMqttSettingsSerializer()
    auth = GatewayConfigAuthSerializer()
    rate_limit_note = serializers.CharField(
        help_text="Operational note: throttle MQTT CONNECT/publish per gateway at the proxy."
    )


class CredentialRotationResponseSerializer(serializers.Serializer):
    """Stage response — the new plaintext is returned exactly once."""

    gateway_slot_id = serializers.CharField()
    username = serializers.CharField()
    password = serializers.CharField(help_text="One-time plaintext; the old hash stays active until ACK.")
    rotation_pending = serializers.BooleanField(help_text="True until commit-on-ACK.")


class RotationAckRequestSerializer(serializers.Serializer):
    new_password = serializers.CharField(
        help_text="The password the device presents as proof it received the rotation."
    )


class RotationAckResponseSerializer(serializers.Serializer):
    gateway_slot_id = serializers.CharField()
    rotation_committed = serializers.BooleanField(help_text="True when the staged hash was promoted.")


class GatewayCommandRequestSerializer(serializers.Serializer):
    """One command to issue. `kind` is the discriminator; `params` is opaque to
    the API and forwarded verbatim in the envelope, because each kind reads a
    different key (`new_password` for rotation, nothing for a reboot)."""

    kind = serializers.ChoiceField(
        choices=["REFRESH_CONFIG", "REBOOT", "DIAGNOSTICS_REQUEST", "ROTATE_CREDENTIAL"],
        help_text="Which command to issue. PROVISION is issued by the bind flow.",
    )
    params = serializers.DictField(
        required=False, default=dict,
        help_text="Kind-specific parameters, forwarded verbatim to the device.",
    )


class GatewayCommandResponseSerializer(serializers.Serializer):
    command_uuid = serializers.UUIDField(help_text="Journal id to poll or reconcile against.")
    gateway_slot_id = serializers.CharField()
    kind = serializers.CharField()
    state = serializers.CharField(help_text="Always PENDING on issue; the broker verdict is async.")
    expires_at = serializers.DateTimeField(help_text="After this the sweep expires the command.")
