"""telemetry OpenAPI components — docs-only serializer set (contract v1.7.0).

No model, no view, no migration. Every shape mirrors what apps/telemetry/views.py
actually returns (`_reading_payload`, the hourly aggregation merge, the metric
catalog) so the generated schema matches backend/openapi.yaml.

Notes that the shapes encode deliberately:

* timestamps are returned as ISO **strings** by the payload builder, not as
  DRF-rendered datetimes — the mirror says CharField so the generated schema
  does not promise a format the view does not apply.
* `buffered_delay_mins` comes from a DB-generated column read via `annotate`;
  it is null whenever the row predates that column (never 0 as a stand-in).
* farm-level rows gain `saloon_id` (string) — the one shape difference from the
  per-saloon read.

فارسی: طرح‌واره‌های مستندسازی تله‌متری — فقط برای OpenAPI، بدون اثر اجرایی.
"""

from rest_framework import serializers


class TelemetryReadingSerializer(serializers.Serializer):
    """One TelemetryReading row (contract v1.4.4 shape)."""

    server_received_ts = serializers.CharField(help_text="ISO timestamp the server stored the row.")
    device_ts = serializers.CharField(help_text="Device-side timestamp (ISO).")
    is_buffered = serializers.BooleanField(
        help_text="True for readings replayed from the gateway's buffer."
    )
    buffered_delay_mins = serializers.FloatField(
        allow_null=True,
        help_text="DB-generated buffer delay; null when the row predates the column.",
    )
    slot_id = serializers.CharField(help_text="Gateway slot that reported the reading.")
    metrics = serializers.DictField(
        child=serializers.DictField(),
        help_text="Per-metric payloads, e.g. {\"temp\": {\"avg\": 24.5, \"min\": …, \"max\": …}}.",
    )
    saloon_id = serializers.CharField(
        required=False,
        help_text="Present on farm-level reads only — the saloon the row came from.",
    )


class SaloonTelemetryResponseSerializer(serializers.Serializer):
    """GET /saloons/{saloon_id}/telemetry/ body.

    Without paging params `results` is chronological ascending; with
    `?limit=`/`?offset=` it is newest-first (so `?limit=1` IS the latest
    reading)."""

    saloon_id = serializers.CharField(help_text="Stringified saloon PK.")
    results = TelemetryReadingSerializer(many=True)


class FarmTelemetryResponseSerializer(serializers.Serializer):
    """GET /farms/{farm_id}/telemetry/ body — the union of the per-saloon streams."""

    farm_id = serializers.IntegerField()
    results = TelemetryReadingSerializer(many=True)


class HourlyAggregatePointSerializer(serializers.Serializer):
    """One chart bucket from metric_hourly_aggregates."""

    bucket_hr = serializers.CharField(
        allow_null=True, required=False, help_text="Hour bucket start (ISO); null on legacy rows."
    )
    avg = serializers.FloatField(
        allow_null=True, required=False, help_text="Sample-count-weighted mean; null when empty."
    )
    high_variance = serializers.BooleanField(
        help_text="True when any source bucket in this hour was flagged high-variance."
    )


class HourlyAnalyticsResponseSerializer(serializers.Serializer):
    """GET /saloons/{saloon_id}/analytics/hourly/ body."""

    metric = serializers.CharField(help_text="Echo of the requested metric key.")
    slot_id = serializers.CharField(
        help_text="Echo of the requested slot; \"\" when buckets are merged saloon-wide."
    )
    results = HourlyAggregatePointSerializer(many=True)


class MetricTypeSerializer(serializers.Serializer):
    """One row of GET /metric-types/ — global sensor catalog (no RLS)."""

    metric_key = serializers.CharField(help_text="Canonical key used on telemetry + thresholds.")
    display_name = serializers.CharField()
    unit = serializers.CharField(allow_null=True, required=False)
    value_type = serializers.CharField(help_text="numeric unless the catalog says otherwise.")
    is_tier1 = serializers.BooleanField(
        help_text="Tier-1 metrics drive the velocity lane and are never muted."
    )
    icon_name = serializers.CharField(allow_null=True, required=False)
