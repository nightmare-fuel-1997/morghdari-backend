"""reports serializers — docs-only DRF shapes for @extend_schema (openapi-annotations).

No model, no queryset, no view logic: every field below mirrors a key the
views in apps.reports.api actually read or return (the §4s merge surface —
canonical names + Flutter input aliases + extras keys + is_completed +
reported_by/reported_at). Field names, required-ness and enums track
backend/openapi.yaml (contract v1.7.0); the generated schema is its mirror.
"""

from rest_framework import serializers


class ChecklistSerializer(serializers.Serializer):
    """The ``checklist{feed, water, device}`` object — nested in writes, and
    the read shape for the lazy ``daily_report_extras`` side-table row
    (a report with no row reads as all-False, the §6 defaults)."""

    feed = serializers.BooleanField(required=False, default=False)
    water = serializers.BooleanField(required=False, default=False)
    device = serializers.BooleanField(required=False, default=False)


class MedicationInputSerializer(serializers.Serializer):
    """One entry of the POST ``medications`` array. Any entry forces the
    FLAGGED branch (D24-8) — status beats an explicit is_draft/status."""

    catalog_item_id = serializers.IntegerField(required=False, allow_null=True)
    medication_name = serializers.CharField(required=False, allow_blank=True)
    dosage = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    frequency = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class MedicationOutSerializer(serializers.Serializer):
    """One entry of the read-side ``medications`` array (single shape)."""

    medication_name = serializers.CharField()
    dosage = serializers.CharField(allow_null=True, required=False)
    frequency = serializers.CharField(allow_null=True, required=False)


class ReportListItemSerializer(serializers.Serializer):
    """§3 merged list item — v1.5 keys keep names/positions, extras keys
    appended, ``is_completed`` derives from status (APPROVED = done)."""

    id = serializers.IntegerField()
    report_date = serializers.DateField(help_text="Calendar day in Asia/Tehran (YYYY-MM-DD).")
    mortality_count = serializers.IntegerField()
    feed_kg = serializers.FloatField(allow_null=True, required=False)
    water_l = serializers.FloatField(allow_null=True, required=False)
    avg_weight_g = serializers.FloatField(allow_null=True, required=False)
    status = serializers.ChoiceField(
        choices=["DRAFT", "SUBMITTED", "FLAGGED", "APPROVED", "REJECTED"]
    )
    bird_count = serializers.IntegerField(allow_null=True, required=False)
    avg_temperature_c = serializers.FloatField(allow_null=True, required=False)
    avg_humidity_pct = serializers.FloatField(allow_null=True, required=False)
    ventilation_status = serializers.ChoiceField(
        choices=["STABLE", "NEEDS_REVIEW", "PROBLEM"], allow_null=True, required=False
    )
    health_status = serializers.ChoiceField(
        choices=["NORMAL", "WARNING", "CRITICAL"], allow_null=True, required=False
    )
    checklist = ChecklistSerializer(required=False)
    is_completed = serializers.BooleanField(help_text="True when status is APPROVED.")


class ReportCreateSerializer(serializers.Serializer):
    """POST /saloons/{saloon_id}/reports/ body — canonical fields plus the
    §2 Flutter input aliases (folded server-side; both-names-sent with
    different values is a 400)."""

    flock_id = serializers.IntegerField()
    report_date = serializers.DateField(help_text="Calendar day in Asia/Tehran (YYYY-MM-DD).")
    mortality_count = serializers.IntegerField(required=False, min_value=0)
    casualties_today = serializers.IntegerField(
        required=False, min_value=0, help_text="Input alias of mortality_count."
    )
    feed_kg = serializers.FloatField(required=False, allow_null=True)
    feed_consumed_kg = serializers.FloatField(
        required=False, allow_null=True, help_text="Input alias of feed_kg."
    )
    water_l = serializers.FloatField(required=False, allow_null=True)
    water_consumed_l = serializers.FloatField(
        required=False, allow_null=True, help_text="Input alias of water_l."
    )
    avg_weight_g = serializers.FloatField(required=False, allow_null=True)
    avg_sample_weight_g = serializers.FloatField(
        required=False, allow_null=True, help_text="Input alias of avg_weight_g."
    )
    status = serializers.ChoiceField(choices=["DRAFT", "SUBMITTED"], required=False)
    is_draft = serializers.BooleanField(
        required=False, help_text="Input alias of status (true→DRAFT, false→SUBMITTED)."
    )
    bird_count = serializers.IntegerField(required=False, min_value=0)
    avg_temperature_c = serializers.FloatField(required=False)
    avg_humidity_pct = serializers.FloatField(required=False, min_value=0, max_value=100)
    ventilation_status = serializers.CharField(
        required=False, help_text="STABLE | NEEDS_REVIEW | PROBLEM (Persian literals accepted)."
    )
    health_status = serializers.CharField(
        required=False, help_text="NORMAL | WARNING | CRITICAL (Persian literals accepted)."
    )
    checklist = ChecklistSerializer(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    medications = MedicationInputSerializer(many=True, required=False)


class ReportEditSerializer(serializers.Serializer):
    """PATCH /reports/{report_id}/ body — same canonical + alias fields as
    POST, minus flock_id/report_date (immutable). Status may only move
    between DRAFT and SUBMITTED here."""

    mortality_count = serializers.IntegerField(required=False, min_value=0)
    casualties_today = serializers.IntegerField(
        required=False, min_value=0, help_text="Input alias of mortality_count."
    )
    feed_kg = serializers.FloatField(required=False, allow_null=True)
    feed_consumed_kg = serializers.FloatField(
        required=False, allow_null=True, help_text="Input alias of feed_kg."
    )
    water_l = serializers.FloatField(required=False, allow_null=True)
    water_consumed_l = serializers.FloatField(
        required=False, allow_null=True, help_text="Input alias of water_l."
    )
    avg_weight_g = serializers.FloatField(required=False, allow_null=True)
    avg_sample_weight_g = serializers.FloatField(
        required=False, allow_null=True, help_text="Input alias of avg_weight_g."
    )
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    status = serializers.ChoiceField(choices=["DRAFT", "SUBMITTED"], required=False)
    is_draft = serializers.BooleanField(
        required=False, help_text="Input alias of status (true→DRAFT, false→SUBMITTED)."
    )
    bird_count = serializers.IntegerField(required=False, min_value=0)
    avg_temperature_c = serializers.FloatField(required=False)
    avg_humidity_pct = serializers.FloatField(required=False, min_value=0, max_value=100)
    ventilation_status = serializers.CharField(
        required=False, help_text="STABLE | NEEDS_REVIEW | PROBLEM (Persian literals accepted)."
    )
    health_status = serializers.CharField(
        required=False, help_text="NORMAL | WARNING | CRITICAL (Persian literals accepted)."
    )
    checklist = ChecklistSerializer(required=False)


class ReportSingleSerializer(ReportListItemSerializer):
    """§3 merged single object — the list item plus notes, medication lines,
    reported_by/reported_at (aliases returned ALONGSIDE submitted_by/at)
    and the review fields."""

    farm_id = serializers.IntegerField()
    saloon_id = serializers.IntegerField()
    flock_id = serializers.IntegerField()
    notes = serializers.CharField(allow_null=True, required=False)
    submitted_by = serializers.IntegerField(allow_null=True, required=False)
    submitted_at = serializers.DateTimeField(allow_null=True, required=False)
    reported_by = serializers.IntegerField(
        allow_null=True, required=False, help_text="Alias of submitted_by (returned alongside)."
    )
    reported_at = serializers.DateTimeField(
        allow_null=True, required=False, help_text="Alias of submitted_at (returned alongside)."
    )
    reviewed_by = serializers.IntegerField(allow_null=True, required=False)
    reviewed_at = serializers.DateTimeField(allow_null=True, required=False)
    review_note = serializers.CharField(allow_null=True, required=False)
    vet_approved_by = serializers.IntegerField(allow_null=True, required=False)
    vet_approved_at = serializers.DateTimeField(allow_null=True, required=False)
    medications = MedicationOutSerializer(many=True, required=False)


class VetReviewRequestSerializer(serializers.Serializer):
    """POST /reports/{report_id}/vet-review/ body — the review decision."""

    decision = serializers.ChoiceField(choices=["APPROVED", "REJECTED"])
    review_note = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class ReportDetailSerializer(serializers.Serializer):
    """The v1.5.0 canonical body — the vet-review response shape (review
    fields included, merge extras excluded)."""

    id = serializers.IntegerField()
    farm_id = serializers.IntegerField()
    saloon_id = serializers.IntegerField()
    flock_id = serializers.IntegerField()
    report_date = serializers.DateField()
    mortality_count = serializers.IntegerField()
    feed_kg = serializers.FloatField(allow_null=True, required=False)
    water_l = serializers.FloatField(allow_null=True, required=False)
    avg_weight_g = serializers.FloatField(allow_null=True, required=False)
    notes = serializers.CharField(allow_null=True, required=False)
    status = serializers.ChoiceField(
        choices=["DRAFT", "SUBMITTED", "FLAGGED", "APPROVED", "REJECTED"]
    )
    submitted_by = serializers.IntegerField(allow_null=True, required=False)
    submitted_at = serializers.DateTimeField(allow_null=True, required=False)
    reviewed_by = serializers.IntegerField(allow_null=True, required=False)
    reviewed_at = serializers.DateTimeField(allow_null=True, required=False)
    review_note = serializers.CharField(allow_null=True, required=False)
    vet_approved_by = serializers.IntegerField(allow_null=True, required=False)
    vet_approved_at = serializers.DateTimeField(allow_null=True, required=False)
