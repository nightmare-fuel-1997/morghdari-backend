"""Farms follow-through components — docs-only serializers (contract v1.7.2).

Same rule as `apps/farms/serializers.py`: every shape here mirrors what
`apps/farms/api_follow.py` actually returns. Update responses deliberately reuse
the READ shapes (`FarmOverview` for a renamed farm, `SaloonDetail` for a renamed
saloon) so a client that already parses the read surface can render a write
response with no second parser.

فارسی: طرح‌واره‌های مستندسازیِ سطحِ نوشتنِ توپولوژی — بدونِ اثرِ اجرایی؛ پاسخِ
ویرایش همان شکلِ خواندن است.
"""

from rest_framework import serializers


class FarmCreateRequestSerializer(serializers.Serializer):
    """POST /farms/ body — the locked `owner creates farms` rule (Tracker §4d)."""

    name = serializers.CharField(max_length=255, help_text="Display name; trimmed.")
    province_id = serializers.IntegerField(help_text="Existing geography province id.")
    city_id = serializers.IntegerField(
        help_text="Existing city id — must belong to `province_id`."
    )
    village_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Optional village — must belong to `city_id`.",
    )


class FarmUpdateRequestSerializer(serializers.Serializer):
    """PATCH /farms/{id}/ body — a name-only edit; geography is immutable."""

    name = serializers.CharField(
        required=False, max_length=255, help_text="New display name (trimmed)."
    )


class SaloonCreateRequestSerializer(serializers.Serializer):
    """POST /farms/{id}/saloons/ body — positions start empty by design."""

    name = serializers.CharField(max_length=255, help_text="Display name; trimmed.")


class SaloonUpdateRequestSerializer(serializers.Serializer):
    """PATCH /saloons/{id}/ body.

    Name-only on purpose: the operational state has its own endpoint (the
    canonical loop owns it) and the threshold-template assignment has O16 — a
    rename can therefore never move a live alarm band.
    """

    name = serializers.CharField(
        required=False, max_length=255, help_text="New display name (trimmed)."
    )


class PositionCreateRequestSerializer(serializers.Serializer):
    """POST /saloons/{id}/positions/ body — `UNIQUE(saloon,label)` stands."""

    label = serializers.CharField(
        max_length=100, help_text='Physical position label, e.g. "North Wall".'
    )


class PositionUpdateRequestSerializer(serializers.Serializer):
    """PATCH /saloons/{id}/positions/{pid}/ body.

    Label rename ONLY. The follow-through draft listed an `is_active` toggle
    here, but `saloon_positions` has no such column and the core schema is
    frozen (additive-only rule) — the erratum is recorded in
    `docs/api-followthrough-v1.md` F8 rather than silently accepted. Sensor
    bindings follow a rename automatically (mapping rows are untouched).
    """

    label = serializers.CharField(required=False, max_length=100)


class PositionItemSerializer(serializers.Serializer):
    position_id = serializers.IntegerField()
    saloon_id = serializers.CharField(help_text="Stringified saloon PK (contract shape).")
    label = serializers.CharField()
    created_by = serializers.IntegerField(allow_null=True, required=False)


class FarmDeleteResponseSerializer(serializers.Serializer):
    farm_id = serializers.IntegerField()
    deleted = serializers.BooleanField(help_text="Always true — a 409 carries the refusal.")


class SaloonDeleteResponseSerializer(serializers.Serializer):
    saloon_id = serializers.CharField(help_text="Stringified saloon PK.")
    deleted = serializers.BooleanField(help_text="Always true — a 409 carries the refusal.")


class FlockCloseRequestSerializer(serializers.Serializer):
    """PATCH /saloons/{id}/flocks/{fid}/close/ body (F14)."""

    end_date = serializers.CharField(
        help_text="`YYYY-MM-DD` or ISO datetime (naive = Asia/Tehran); ≥ the cycle's start."
    )


class FlockCloseResponseSerializer(serializers.Serializer):
    flock_id = serializers.IntegerField()
    saloon_id = serializers.CharField(help_text="Stringified saloon PK.")
    end_date = serializers.DateField(help_text="Tehran calendar day the cycle closed.")
    status = serializers.CharField(help_text='Always "CLOSED".')


class MortalityCreateRequestSerializer(serializers.Serializer):
    """POST /saloons/{id}/flocks/{fid}/mortality/ body (F15).

    Direct entry BESIDE the daily-report counts, not instead of them: this is
    the writer `mortality_records` never had, and it is the table the KPI's
    `mortality_pct` / `daily_mortality_pct` series reads. `cause` is an
    additive extension of the draft (the column exists and was unused).
    """

    occurred_on = serializers.CharField(
        help_text="`YYYY-MM-DD` (Tehran day). Not in the future, not before the cycle start."
    )
    count = serializers.IntegerField(min_value=1, help_text="Deaths on that day (≥ 1).")
    note = serializers.CharField(
        required=False, allow_blank=True, help_text="Free-text note (column `notes`)."
    )
    cause = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=100,
        help_text='Optional cause, e.g. "ammonia_toxicity", "heat_stress", "culling".',
    )


class MortalityResponseSerializer(serializers.Serializer):
    mortality_id = serializers.IntegerField()
    flock_id = serializers.IntegerField()
    saloon_id = serializers.CharField(help_text="Stringified saloon PK.")
    count = serializers.IntegerField()
    occurred_on = serializers.DateField(help_text="Tehran calendar day (stored at 00:00).")
    recorded_by = serializers.IntegerField(help_text="Recording user id.")
    cause = serializers.CharField(allow_null=True, required=False)
    notes = serializers.CharField(allow_null=True, required=False)


class OverviewFarmSerializer(serializers.Serializer):
    farm_id = serializers.IntegerField()
    name = serializers.CharField()
    saloon_count = serializers.IntegerField()
    active_alert_count = serializers.IntegerField(
        help_text="Unresolved alerts across the farm's saloons (any tier)."
    )
    highest_tier = serializers.IntegerField(
        allow_null=True, required=False, help_text="Null when nothing is unresolved."
    )


class OverviewTotalsSerializer(serializers.Serializer):
    farm_count = serializers.IntegerField()
    saloon_count = serializers.IntegerField()
    active_alert_count = serializers.IntegerField()
    highest_tier = serializers.IntegerField(allow_null=True, required=False)


class OverviewResponseSerializer(serializers.Serializer):
    """GET /overview/ — the owner home rollup, RLS- and scope-narrowed."""

    farms = OverviewFarmSerializer(many=True)
    totals = OverviewTotalsSerializer()
