"""farms OpenAPI components — docs-only serializer set (contract v1.7.0).

No model, no view, no migration. Every shape below mirrors what apps/farms/api.py
actually returns (the `_saloon_detail_payload` / `_farm_overview_payload` /
`_flock_*` builders), so the generated schema matches backend/openapi.yaml
instead of inventing shapes.

Two contract conventions are encoded here on purpose:

* saloon/org ids travel as **strings** (`str(pk)` / `str(org_id)`) while farm
  and flock ids are integers (AutoField PKs) — see the payload builders.
* `gateway_status` / `gateway_slot_id` / `flock_age_days` are honestly nullable:
  Redis being down, a saloon without a gateway or a saloon without an open flock
  all read `null`, never a fabricated value (D22 degradation rule).

فارسی: طرح‌واره‌های مستندسازی مزرعه و سالن — فقط برای OpenAPI، بدون اثر اجرایی.
"""

from rest_framework import serializers

OPERATIONAL_STATES = [
    "EMPTY_SANITATION",
    "PRE_HEATING",
    "ACTIVE_FLOCK",
    "HARVESTED_VACANT",
]


class LocationsSerializer(serializers.Serializer):
    """Farm geography — village is optional (a farm may sit in a city alone)."""

    province_id = serializers.IntegerField()
    province_name = serializers.CharField()
    city_id = serializers.IntegerField()
    city_name = serializers.CharField()
    village_id = serializers.IntegerField(allow_null=True, required=False)
    village_name = serializers.CharField(allow_null=True, required=False)


class SensorSlotSerializer(serializers.Serializer):
    """One SaloonSensorSlot row as the detail payload renders it."""

    slot_id = serializers.CharField(help_text="Sensor slot id (device-assigned string).")
    position_id = serializers.IntegerField(allow_null=True, required=False)
    position_label = serializers.CharField(allow_null=True, required=False)
    is_active = serializers.BooleanField(
        help_text="Inactive slots keep their history but leave the live-metric set."
    )


class SaloonDetailSerializer(serializers.Serializer):
    """SaloonDetail — GET /saloons/{saloon_id}/ and every row of GET /saloons/."""

    saloon_id = serializers.CharField(help_text="Stringified saloon PK (contract shape).")
    name = serializers.CharField()
    farm_id = serializers.IntegerField()
    farm_name = serializers.CharField()
    locations = LocationsSerializer()
    org_id = serializers.CharField(help_text="Stringified org UUID.")
    operational_state = serializers.ChoiceField(
        choices=OPERATIONAL_STATES,
        help_text="Physical truth: a saloon never transitioned reads EMPTY_SANITATION.",
    )
    flock_age_days = serializers.IntegerField(
        allow_null=True,
        required=False,
        help_text="Whole days in Asia/Tehran since the open flock started; null without one.",
    )
    gateway_slot_id = serializers.CharField(
        allow_null=True, required=False, help_text="Null when no zone is assigned."
    )
    gateway_status = serializers.CharField(
        allow_null=True,
        required=False,
        help_text="ONLINE/OFFLINE from the gateway:status key; null when Redis is unavailable.",
    )
    sensor_slots = SensorSlotSerializer(many=True)


class FarmSaloonCardSerializer(serializers.Serializer):
    """One card of FarmOverview.saloons — the dashboard's per-saloon rollup."""

    saloon_id = serializers.CharField(help_text="Stringified saloon PK.")
    name = serializers.CharField()
    operational_state = serializers.ChoiceField(choices=OPERATIONAL_STATES)
    flock_age_days = serializers.IntegerField(allow_null=True, required=False)
    gateway_status = serializers.CharField(allow_null=True, required=False)
    gateway_slot_id = serializers.CharField(allow_null=True, required=False)
    active_alert_count = serializers.IntegerField(
        help_text="Unresolved alerts for the saloon (any tier)."
    )
    highest_active_tier = serializers.IntegerField(
        allow_null=True,
        required=False,
        help_text="Highest tier among the unresolved alerts; null when none.",
    )


class FarmOverviewSerializer(serializers.Serializer):
    """FarmOverview — GET /farms/{farm_id}/overview/ and every row of GET /farms/."""

    farm_id = serializers.IntegerField()
    name = serializers.CharField()
    locations = LocationsSerializer()
    saloons = FarmSaloonCardSerializer(many=True)


class FlockCycleSerializer(serializers.Serializer):
    """One row of GET /saloons/{saloon_id}/flocks/ — dates are Tehran calendar days."""

    flock_id = serializers.IntegerField()
    breed_type = serializers.CharField()
    start_date = serializers.DateField(help_text="Tehran calendar day, YYYY-MM-DD.")
    end_date = serializers.DateField(
        allow_null=True, required=False, help_text="Null while the flock is open."
    )


class FlockCycleCreateSerializer(serializers.Serializer):
    """POST /saloons/{saloon_id}/flocks/ body (contract v1.4.2).

    `placement_count` is REQUIRED and ≥1 — it is the mortality-% denominator,
    never a default. `start_date` accepts a plain day or a full ISO datetime
    (a naive datetime is read as Asia/Tehran)."""

    breed_type = serializers.CharField()
    start_date = serializers.CharField(
        help_text="`YYYY-MM-DD` or ISO datetime (naive = Asia/Tehran)."
    )
    placement_count = serializers.IntegerField(
        min_value=1, help_text="Chicks placed at flock start — mortality-% denominator."
    )
    day_old_weight_g = serializers.FloatField(
        required=False, allow_null=True, help_text="Optional day-old chick weight for FCR."
    )


class FlockCycleCreateResponseSerializer(FlockCycleSerializer):
    """201 body — the created cycle plus the placement count that was stored."""

    placement_count = serializers.IntegerField()


class OperationalStateRequestSerializer(serializers.Serializer):
    """POST /saloons/{saloon_id}/operational-state/ body — one step of the loop."""

    state = serializers.ChoiceField(
        choices=OPERATIONAL_STATES,
        help_text=(
            "Target state. Only one forward step is legal: "
            "EMPTY_SANITATION→PRE_HEATING→ACTIVE_FLOCK→HARVESTED_VACANT→EMPTY_SANITATION."
        ),
    )
    reason = serializers.CharField(
        required=False, allow_blank=True, help_text="Optional operator note stored on the row."
    )


class OperationalStateResponseSerializer(serializers.Serializer):
    """200 body — the state the saloon now holds."""

    saloon_id = serializers.CharField(help_text="Stringified saloon PK.")
    operational_state = serializers.ChoiceField(choices=OPERATIONAL_STATES)
