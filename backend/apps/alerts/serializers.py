"""alerts OpenAPI components — docs-only serializer set (contract v1.5.0).

No model, no view, no migration. Every shape below mirrors what the views in
views.py / kpi_views.py / production_views.py actually return (verified
against the ``_out`` helpers and the Response bodies), so the generated
schema matches backend/openapi.yaml instead of inventing shapes.

Metric keys reuse the canonical catalog (temp/humidity/nh3/thi/co/
power_mains/power_gen); ids and dates reuse realistic contract values
(integer saloon/flock ids, YYYY-MM-DD entry dates, +03:30 datetimes).

فارسی: طرحواره‌های مستندسازی هشدارها و KPI — فقط برای OpenAPI، بدون اثر اجرایی.
"""

from drf_spectacular.utils import OpenApiExample
from rest_framework import serializers

METRIC_TYPE_CHOICES = [
    "thi",
    "co",
    "power_mains",
    "power_gen",
    "temp",
    "humidity",
    "nh3",
    "lux",
]
ALERT_STATUS_CHOICES = ["ACTIVE_TIER_1", "ACTIVE_TIER_2", "ACTIVE_TIER_3", "RESOLVED"]
ALERT_SOURCE_CHOICES = ["REALTIME", "REPLAY_ANALYSIS"]
ALERT_STATUS_FILTER_CHOICES = ["ACTIVE", "ALL"]
FEED_TYPE_CHOICES = ["starter", "grower", "finisher"]
INTAKE_SOURCE_CHOICES = ["manual", "device"]
BIOSECURITY_EVENT_CHOICES = [
    "visitor_entry",
    "vehicle_entry",
    "disinfection",
    "rodent_control",
    "outbreak_prep",
    "other",
]
KPI_METRIC_CHOICES = ["mortality_pct", "compliance_pct", "fcr", "cycle_summary"]
DASHBOARD_ROLE_CHOICES = ["owner", "manager", "engineer", "doctor", "worker"]
DASHBOARD_WIDGET_KEYS = [
    "live_readings",
    "mortality_pct",
    "compliance_pct",
    "fcr",
    "cycle_summary",
    "alerts",
    "feed_intake",
    "weight_trend",
]
RESOLVED_FROM_CHOICES = ["personal", "role_default", "system_default"]


# ---------------------------------------------------------------------------
# Saloon alerts (views.py :: saloon_alerts / alert_acknowledge)
# ---------------------------------------------------------------------------


class AlertInstanceSerializer(serializers.Serializer):
    """One alert row — mirrors views._alert_out exactly."""

    alert_id = serializers.UUIDField(help_text="Alert instance id (uuid).")
    metric_type = serializers.ChoiceField(
        choices=METRIC_TYPE_CHOICES,
        help_text=(
            "Breach metric. Tier-1 velocity set (nh3 incl. max_ts velocity, "
            "temp velocity) always pages and is never muted."
        ),
    )
    current_status = serializers.ChoiceField(choices=ALERT_STATUS_CHOICES)
    current_value = serializers.FloatField(
        allow_null=True,
        help_text="Last observed breach value; null for legacy/replay rows.",
    )
    source = serializers.ChoiceField(choices=ALERT_SOURCE_CHOICES)
    triggered_at = serializers.DateTimeField(allow_null=True, required=False)
    resolved_at = serializers.DateTimeField(
        allow_null=True,
        required=False,
        help_text="Null while the alert is still active.",
    )
    highest_tier_reached = serializers.IntegerField(
        min_value=1, max_value=3, help_text="Highest escalation tier reached (1-3)."
    )
    reason = serializers.CharField(
        max_length=200,
        allow_null=True,
        allow_blank=True,
        required=False,
        help_text="Why the alert fired: velocity emergency, sustained threshold breach, sensor loss, etc.",
    )


class SaloonAlertsResponseSerializer(serializers.Serializer):
    results = AlertInstanceSerializer(
        many=True, help_text="Alerts newest-first (cross-org reads are an empty 200)."
    )


class AlertAcknowledgeRequestSerializer(serializers.Serializer):
    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional operator note stored on the acknowledgement row.",
    )


class AlertAcknowledgeResponseSerializer(serializers.Serializer):
    alert_id = serializers.UUIDField()
    acknowledged_by = serializers.IntegerField(help_text="Acknowledging user id.")
    pause_minutes = serializers.IntegerField(
        help_text="Escalation pause window (default 10 min from system configuration)."
    )
    status = serializers.CharField(help_text='Always "ACKNOWLEDGED".')


ALERT_LIST_RESPONSE_EXAMPLE = OpenApiExample(
    "Active saloon alerts",
    response_only=True,
    value={
        "results": [
            {
                "alert_id": "3f7c9e2a-1b4d-4e6f-8a2c-5d7f9b3e6a1c",
                "metric_type": "nh3",
                "current_status": "ACTIVE_TIER_1",
                "current_value": 28.5,
                "source": "REALTIME",
                "triggered_at": "2026-08-20T14:32:00+03:30",
                "resolved_at": None,
                "highest_tier_reached": 1,
                "reason": "NH3 velocity emergency: max_ts=52 >= 50",
            },
            {
                "alert_id": "7a1e4c90-2f5b-4d8e-9c3a-6e8f1b4d7a2e",
                "metric_type": "temp",
                "current_status": "ACTIVE_TIER_2",
                "current_value": 33.8,
                "source": "REALTIME",
                "triggered_at": "2026-08-20T13:05:00+03:30",
                "resolved_at": None,
                "highest_tier_reached": 2,
                "reason": "Sustained threshold breach: temp avg 33.80 >= 26.00",
            },
        ]
    },
)

ALERT_ACK_REQUEST_EXAMPLE = OpenApiExample(
    "Acknowledge with note",
    request_only=True,
    value={"notes": "Checked ventilation, NH3 falling."},
)

ALERT_ACK_RESPONSE_EXAMPLE = OpenApiExample(
    "Acknowledged",
    response_only=True,
    value={
        "alert_id": "3f7c9e2a-1b4d-4e6f-8a2c-5d7f9b3e6a1c",
        "acknowledged_by": 42,
        "pause_minutes": 10,
        "status": "ACKNOWLEDGED",
    },
)


# ---------------------------------------------------------------------------
# Weight samples (kpi_views.py :: weight_samples)
# ---------------------------------------------------------------------------


class WeightSampleSerializer(serializers.Serializer):
    """GET item / POST 201 body — mirrors kpi_views._sample_out."""

    sampled_at = serializers.DateTimeField(
        help_text="Sampling moment (naive input is read as Asia/Tehran)."
    )
    sample_size = serializers.IntegerField(
        min_value=1, help_text="Birds weighed in this sampling event."
    )
    avg_weight_g = serializers.FloatField(help_text="Mean weight in grams.")
    uniformity_pct = serializers.FloatField(
        allow_null=True,
        required=False,
        help_text="% of birds within ±10% of the mean; null when not recorded.",
    )
    source = serializers.ChoiceField(
        choices=INTAKE_SOURCE_CHOICES,
        help_text="'manual' in v1; 'device' reserved for weigh stations.",
    )


class WeightSampleCreateSerializer(serializers.Serializer):
    sample_size = serializers.IntegerField(min_value=1)
    avg_weight_g = serializers.FloatField()
    min_weight_g = serializers.FloatField(required=False)
    max_weight_g = serializers.FloatField(required=False)
    uniformity_pct = serializers.FloatField(required=False)
    sampled_at = serializers.DateTimeField(
        required=False, help_text="Defaults to now when omitted."
    )
    notes = serializers.CharField(required=False, allow_blank=True)


WEIGHT_SAMPLE_CREATE_EXAMPLE = OpenApiExample(
    "Log weight sample",
    request_only=True,
    value={
        "sample_size": 50,
        "avg_weight_g": 1850.5,
        "min_weight_g": 1620.0,
        "max_weight_g": 2105.0,
        "uniformity_pct": 88.5,
        "sampled_at": "2026-08-18T09:30:00+03:30",
        "notes": "Week-4 sampling, north half.",
    },
)

WEIGHT_SAMPLE_RESPONSE_EXAMPLE = OpenApiExample(
    "Weight sample",
    response_only=True,
    value={
        "sampled_at": "2026-08-18T09:30:00+03:30",
        "sample_size": 50,
        "avg_weight_g": 1850.5,
        "uniformity_pct": 88.5,
        "source": "manual",
    },
)


# ---------------------------------------------------------------------------
# Feed intake (kpi_views.py :: feed_intake)
# ---------------------------------------------------------------------------


class FeedIntakeEntrySerializer(serializers.Serializer):
    entry_date = serializers.DateField(help_text="Persian-calendar day (YYYY-MM-DD).")
    feed_kg = serializers.FloatField(help_text="Consumed = delivered − remaining.")
    feed_type = serializers.ChoiceField(
        choices=FEED_TYPE_CHOICES, allow_null=True, required=False
    )
    source = serializers.ChoiceField(choices=INTAKE_SOURCE_CHOICES)


class FeedIntakeUpsertRequestSerializer(serializers.Serializer):
    entry_date = serializers.DateField()
    feed_kg = serializers.FloatField()
    feed_type = serializers.ChoiceField(choices=FEED_TYPE_CHOICES, required=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class FeedIntakeUpsertResponseSerializer(serializers.Serializer):
    entry_date = serializers.DateField()
    feed_kg = serializers.FloatField()
    feed_type = serializers.ChoiceField(
        choices=FEED_TYPE_CHOICES, allow_null=True, required=False
    )
    source = serializers.ChoiceField(choices=INTAKE_SOURCE_CHOICES)
    corrected = serializers.BooleanField(
        help_text="True when the date already had a row (UPDATE, audited)."
    )


FEED_INTAKE_UPSERT_REQUEST_EXAMPLE = OpenApiExample(
    "Upsert feed intake",
    request_only=True,
    value={
        "entry_date": "2026-08-18",
        "feed_kg": 125.5,
        "feed_type": "grower",
        "notes": "Evening top-up included.",
    },
)

FEED_INTAKE_UPSERT_RESPONSE_EXAMPLE = OpenApiExample(
    "Feed intake upserted",
    response_only=True,
    value={
        "entry_date": "2026-08-18",
        "feed_kg": 125.5,
        "feed_type": "grower",
        "source": "manual",
        "corrected": False,
    },
)


# ---------------------------------------------------------------------------
# Water intake (kpi_views.py :: water_intake)
# ---------------------------------------------------------------------------


class WaterIntakeEntrySerializer(serializers.Serializer):
    entry_date = serializers.DateField()
    source = serializers.ChoiceField(choices=INTAKE_SOURCE_CHOICES)
    water_l = serializers.FloatField(help_text="Litres consumed that day.")


class WaterIntakeUpsertRequestSerializer(serializers.Serializer):
    entry_date = serializers.DateField()
    water_l = serializers.FloatField()
    notes = serializers.CharField(required=False, allow_blank=True)


class WaterIntakeUpsertResponseSerializer(serializers.Serializer):
    entry_date = serializers.DateField()
    water_l = serializers.FloatField()
    source = serializers.ChoiceField(choices=INTAKE_SOURCE_CHOICES)
    corrected = serializers.BooleanField(
        help_text="True when the date already had a row (UPDATE, audited)."
    )


WATER_INTAKE_UPSERT_REQUEST_EXAMPLE = OpenApiExample(
    "Upsert water intake",
    request_only=True,
    value={"entry_date": "2026-08-18", "water_l": 210.0},
)

WATER_INTAKE_UPSERT_RESPONSE_EXAMPLE = OpenApiExample(
    "Water intake upserted",
    response_only=True,
    value={
        "entry_date": "2026-08-18",
        "water_l": 210.0,
        "source": "manual",
        "corrected": False,
    },
)

WATER_INTAKE_LIST_RESPONSE_EXAMPLE = OpenApiExample(
    "Water intake entries",
    response_only=True,
    value=[
        {
            "entry_date": "2026-08-18",
            "source": "manual",
            "water_l": 210.0,
        },
        {
            "entry_date": "2026-08-17",
            "source": "manual",
            "water_l": 205.5,
        },
    ],
)


# ---------------------------------------------------------------------------
# Saloon KPIs (kpi_views.py :: saloon_kpis)
# ---------------------------------------------------------------------------


class DailyMortalityPointSerializer(serializers.Serializer):
    date = serializers.DateField()
    pct = serializers.FloatField(
        allow_null=True, help_text="That day's deaths / birds alive, in percent."
    )


class CycleSummarySerializer(serializers.Serializer):
    days_elapsed = serializers.IntegerField()
    cumulative_mortality_pct = serializers.FloatField(allow_null=True, required=False)
    latest_fcr = serializers.FloatField(
        allow_null=True,
        required=False,
        help_text="Null until BOTH a weight sample and feed data exist — never 0.",
    )
    alert_count = serializers.IntegerField(help_text="REALTIME alerts for the saloon.")
    compliance_pct = serializers.FloatField(allow_null=True, required=False)


class SaloonKpisResponseSerializer(serializers.Serializer):
    flock_id = serializers.IntegerField(help_text="Resolved flock (explicit or active).")
    mortality_pct = serializers.FloatField(
        allow_null=True, required=False, help_text="Cumulative mortality in percent."
    )
    daily_mortality_pct = DailyMortalityPointSerializer(
        many=True, required=False, help_text="Per-day series over from/to."
    )
    compliance_pct = serializers.FloatField(
        allow_null=True,
        required=False,
        help_text="Share of hours with all banded metrics in band.",
    )
    fcr = serializers.FloatField(
        allow_null=True,
        required=False,
        help_text="Feed conversion ratio; null until weight + feed data exist.",
    )
    cycle_summary = CycleSummarySerializer(required=False)


SALOON_KPIS_RESPONSE_EXAMPLE = OpenApiExample(
    "KPI bundle",
    response_only=True,
    value={
        "flock_id": 34,
        "mortality_pct": 2.35,
        "daily_mortality_pct": [
            {"date": "2026-08-17", "pct": 0.12},
            {"date": "2026-08-18", "pct": 0.08},
        ],
        "compliance_pct": 91.4,
        "fcr": 1.62,
        "cycle_summary": {
            "days_elapsed": 24,
            "cumulative_mortality_pct": 2.35,
            "latest_fcr": 1.62,
            "alert_count": 3,
            "compliance_pct": 91.4,
        },
    },
)


# ---------------------------------------------------------------------------
# Dashboard config (kpi_views.py :: me_dashboard_config / role_dashboard_config)
# ---------------------------------------------------------------------------


class DashboardWidgetsSerializer(serializers.Serializer):
    """System widget keys — partial updates allowed (missing keys keep resolution)."""

    live_readings = serializers.BooleanField(required=False)
    mortality_pct = serializers.BooleanField(required=False)
    compliance_pct = serializers.BooleanField(required=False)
    fcr = serializers.BooleanField(required=False)
    cycle_summary = serializers.BooleanField(required=False)
    alerts = serializers.BooleanField(required=False)
    feed_intake = serializers.BooleanField(required=False)
    weight_trend = serializers.BooleanField(required=False)


class DashboardConfigUpdateRequestSerializer(serializers.Serializer):
    widgets = DashboardWidgetsSerializer(
        help_text="Partial map of known widget keys to booleans."
    )


class ResolvedDashboardConfigSerializer(serializers.Serializer):
    role = serializers.ChoiceField(
        choices=DASHBOARD_ROLE_CHOICES, help_text="Caller's current role context."
    )
    widgets = DashboardWidgetsSerializer(help_text="Resolved widget set.")
    resolved_from = serializers.DictField(
        child=serializers.ChoiceField(choices=RESOLVED_FROM_CHOICES),
        help_text="Per-widget provenance layer.",
    )


class RoleDashboardConfigSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=DASHBOARD_ROLE_CHOICES)
    widgets = DashboardWidgetsSerializer()
    is_system_default = serializers.BooleanField(
        help_text="True when no role row exists (system defaults shown)."
    )


DASHBOARD_UPDATE_REQUEST_EXAMPLE = OpenApiExample(
    "Widget overrides",
    request_only=True,
    value={"widgets": {"alerts": True, "weight_trend": False}},
)

RESOLVED_DASHBOARD_RESPONSE_EXAMPLE = OpenApiExample(
    "Resolved dashboard config",
    response_only=True,
    value={
        "role": "engineer",
        "widgets": {
            "live_readings": True,
            "mortality_pct": True,
            "compliance_pct": True,
            "fcr": True,
            "cycle_summary": True,
            "alerts": True,
            "feed_intake": True,
            "weight_trend": False,
        },
        "resolved_from": {
            "live_readings": "system_default",
            "mortality_pct": "system_default",
            "compliance_pct": "system_default",
            "fcr": "system_default",
            "cycle_summary": "system_default",
            "alerts": "role_default",
            "feed_intake": "system_default",
            "weight_trend": "personal",
        },
    },
)

ROLE_DASHBOARD_RESPONSE_EXAMPLE = OpenApiExample(
    "Role default config",
    response_only=True,
    value={
        "role": "engineer",
        "widgets": {
            "live_readings": True,
            "mortality_pct": True,
            "compliance_pct": True,
            "fcr": True,
            "cycle_summary": True,
            "alerts": True,
            "feed_intake": True,
            "weight_trend": True,
        },
        "is_system_default": True,
    },
)


# ---------------------------------------------------------------------------
# Biosecurity events (production_views.py :: biosecurity_events)
# ---------------------------------------------------------------------------


class BiosecurityEventSerializer(serializers.Serializer):
    """GET item / POST 201 body — mirrors production_views._bio_event_out."""

    id = serializers.IntegerField()
    event_type = serializers.ChoiceField(choices=BIOSECURITY_EVENT_CHOICES)
    occurred_at = serializers.DateTimeField(
        help_text="Naive input is read as Asia/Tehran."
    )
    person_count = serializers.IntegerField(allow_null=True, required=False)
    recorded_by = serializers.IntegerField(help_text="Recording user id.")
    details = serializers.DictField(
        allow_null=True,
        required=False,
        help_text="Free-form evidence, e.g. {origin, purpose, product_used}.",
    )


class BiosecurityEventCreateRequestSerializer(serializers.Serializer):
    event_type = serializers.ChoiceField(choices=BIOSECURITY_EVENT_CHOICES)
    occurred_at = serializers.DateTimeField(
        required=False, help_text="Defaults to now when omitted."
    )
    person_count = serializers.IntegerField(required=False, allow_null=True)
    details = serializers.DictField(required=False, allow_null=True)


BIOSECURITY_CREATE_REQUEST_EXAMPLE = OpenApiExample(
    "Record biosecurity event",
    request_only=True,
    value={
        "event_type": "visitor_entry",
        "occurred_at": "2026-08-19T08:15:00+03:30",
        "person_count": 3,
        "details": {"origin": "Karaj hatchery", "purpose": "veterinary visit"},
    },
)

BIOSECURITY_EVENT_RESPONSE_EXAMPLE = OpenApiExample(
    "Biosecurity event",
    response_only=True,
    value={
        "id": 11,
        "event_type": "visitor_entry",
        "occurred_at": "2026-08-19T08:15:00+03:30",
        "person_count": 3,
        "recorded_by": 42,
        "details": {"origin": "Karaj hatchery", "purpose": "veterinary visit"},
    },
)


# ---------------------------------------------------------------------------
# Capabilities (production_views.py :: saloon_capabilities)
# ---------------------------------------------------------------------------


class CapabilityFeatureSerializer(serializers.Serializer):
    feature = serializers.CharField(
        help_text="Feature key from FEATURE_MATRIX (fcr, water_ratio, thi, co, power, biosecurity_log, mortality_pct, compliance_pct)."
    )
    available = serializers.BooleanField(
        help_text="Never 'normal', never 0 — unavailable features explain why."
    )
    missing_inputs = serializers.ListField(
        child=serializers.CharField(),
        help_text="Live metric keys / log labels / computed facts still missing.",
    )
    enable_path = serializers.CharField(
        allow_blank=True, help_text="Operator action; empty when available."
    )


class SaloonCapabilitiesResponseSerializer(serializers.Serializer):
    saloon_id = serializers.IntegerField()
    live_metrics = serializers.ListField(
        child=serializers.CharField(),
        help_text="allowed_metric_keys of ACTIVE slots ∪ keys seen in recent telemetry.",
    )
    features = CapabilityFeatureSerializer(many=True)


SALOON_CAPABILITIES_RESPONSE_EXAMPLE = OpenApiExample(
    "Capability profile",
    response_only=True,
    value={
        "saloon_id": 12,
        "live_metrics": ["humidity", "nh3", "temp"],
        "features": [
            {
                "feature": "fcr",
                "available": False,
                "missing_inputs": ["weight samples"],
                "enable_path": "Log a weight sample or connect a weigh station",
            },
            {
                "feature": "thi",
                "available": True,
                "missing_inputs": [],
                "enable_path": "",
            },
            {
                "feature": "power",
                "available": False,
                "missing_inputs": ["power_mains", "power_gen"],
                "enable_path": "Connect mains or generator power monitoring",
            },
        ],
    },
)
