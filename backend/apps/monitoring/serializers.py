"""Monitoring response serializers — docs-only (drf-spectacular).

Real DRF serializers mirroring the shapes views.py actually returns
(contract backend/openapi.yaml: HealthCheckResponse + {unix_ts}).
No model, no view, no migration. Views import these for @extend_schema
responses only — runtime bodies are unchanged.
"""

from rest_framework import serializers


class HealthDatabaseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="ok | error — SELECT 1 probe.")
    latency_ms = serializers.FloatField(
        allow_null=True, help_text="Probe latency; null when the probe failed."
    )
    detail = serializers.CharField(
        required=False, allow_blank=True, help_text="Truncated probe error (error only)."
    )


class HealthRedisQueueDepthSerializer(serializers.Serializer):
    realtime = serializers.IntegerField(
        allow_null=True, help_text="XLEN telemetry:realtime; null when unreadable."
    )
    historical = serializers.IntegerField(
        allow_null=True, help_text="XLEN telemetry:historical; null when unreadable."
    )
    pending_approval = serializers.IntegerField(
        allow_null=True,
        help_text="Unapproved-gateway buffer — null until its producer exists (honest missing).",
    )


class HealthRedisSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="ok | error — PING probe.")
    ping_ms = serializers.FloatField(
        allow_null=True, help_text="Ping latency; null when the probe failed."
    )
    detail = serializers.CharField(
        required=False, allow_blank=True, help_text="Truncated probe error (error only)."
    )
    queue_depth = HealthRedisQueueDepthSerializer()
    memory_usage_mb = serializers.FloatField(
        allow_null=True,
        help_text="Reserved gauge — null until its producer exists (never fabricated zero).",
    )
    insert_buffer_length = serializers.IntegerField(
        allow_null=True,
        help_text="Rows awaiting batch flush — null until its producer exists.",
    )


class HealthSubscriberSerializer(serializers.Serializer):
    group_members = serializers.IntegerField(
        allow_null=True,
        help_text="Live shared-subscription members — null until its producer exists.",
    )


class HealthCelerySerializer(serializers.Serializer):
    status = serializers.CharField(
        allow_null=True, help_text="Worker status — null until its producer exists."
    )
    active_workers = serializers.IntegerField(
        allow_null=True, help_text="Active workers — null until its producer exists."
    )


class HealthServicesSerializer(serializers.Serializer):
    database = HealthDatabaseSerializer()
    redis = HealthRedisSerializer()
    subscriber = HealthSubscriberSerializer()
    celery = HealthCelerySerializer()


class HealthResponseSerializer(serializers.Serializer):
    """Contract HealthCheckResponse — healthy=200 / degraded=503."""

    status = serializers.ChoiceField(
        choices=["healthy", "degraded"],
        help_text="degraded when the DATABASE or REDIS probe fails.",
    )
    version = serializers.CharField(help_text="App version (views.APP_VERSION).")
    timestamp = serializers.CharField(
        help_text="Server time, UTC ISO Z (e.g. 2026-09-06T12:00:00Z)."
    )
    services = HealthServicesSerializer()


class HardwareTimeSerializer(serializers.Serializer):
    """NTP fallback — current server time in whole unix seconds."""

    unix_ts = serializers.IntegerField(help_text="Server unix time (seconds).")
