"""
Seed health checks (TASKS M1-07 acceptance).

فارسی: سلامتِ بذرها — تعداد و محتوای کلیدی کاتالوگ/پیکربندی/سهمیه‌ها چک
می‌شود تا خرابیِ seed بی‌صدا سبز نماند.

Runs only where the seeds can truly exist (postgres + the seeded_rls_db
fixture); sqlite laptop loops skip — CI is the integration truth.
"""

import pytest

from apps.geography.models import City, Province, Village
from apps.system_config.models import SystemConfiguration, TierQuotaMapping
from apps.telemetry.models import SensorMetricType

pytestmark = [
    pytest.mark.use_migrations,  # declared intent: these tests need real SQL artifacts
    pytest.mark.django_db,
]

EXPECTED_METRIC_KEYS = {
    "temp", "humidity", "lux", "nh3",
    "water_l", "co", "power_mains", "power_gen",
}
EXPECTED_CONFIG_KEYS = {
    "replay_rate_limit_per_min",
    "org_offboard_grace_days",
    "replay_rate_limit_per_org_per_min",
    "quota_exempt_tier1_metrics",
    "dedup_window_sec",
    "los_watchdog_interval_sec",
    "clock_drift_alert_threshold_min",
    "threshold_transition_grace_sec",
    "cmd_timeout_ttl_sec",
    "nh3_max_ts_velocity_threshold",
    "telemetry_insert_batch_size",
    "subscriber_mqueue_len",
    "hysteresis_buffer_units",
    "active_flock_temp_tolerance_c",
    "dlq_alert_threshold",
    "alert_acknowledgement_pause_minutes",
    "ws_max_connections_per_user",
    "mqtt_cert_renewal_interval_days",
    "max_pending_gateway_requests",
    "los_suppress_after_redis_restart_min",
    "db_connections_alert_pct",
    "temp_velocity_delta_60s",
    "metric_autodiscover_min_windows",
    "farm_power_outage_los_ratio",
    "farm_power_outage_window_min",
    "correlation_suppress_minutes",
}


def test_metric_catalog_complete(seeded_rls_db):
    keys = set(SensorMetricType.objects.values_list("metric_key", flat=True))
    assert keys == EXPECTED_METRIC_KEYS
    # Tier-1 safety flags drive velocity emergencies — wrong flags are a
    # safety regression, not a cosmetic one.
    tier1 = set(SensorMetricType.objects.filter(is_tier1=True).values_list("metric_key", flat=True))
    assert tier1 == {"temp", "nh3", "co"}


def test_system_configuration_canonical_keys(seeded_rls_db):
    keys = set(SystemConfiguration.objects.values_list("key", flat=True))
    assert keys == EXPECTED_CONFIG_KEYS  # exact set: extra keys would mean doc drift
    assert SystemConfiguration.objects.get(key="dedup_window_sec").value == "90000"


def test_tier_quotas_verbatim(seeded_rls_db):
    quotas = {q.tier: (q.max_farms, q.max_saloons, q.max_sensor_slots, q.max_hourly_messages, q.max_gateways)
              for q in TierQuotaMapping.objects.all()}
    assert quotas == {
        "basic": (5, 20, 50, 10000, 2),
        "pro": (20, 100, 250, 50000, 10),
        "enterprise": (9999, 9999, 9999, 999999, 9999),
    }


def test_geo_sample_usable(seeded_rls_db):
    # Build-provided sample (not from docs) — just enough for FK targets.
    assert Province.objects.count() >= 3
    assert City.objects.count() >= 3
    assert Village.objects.count() >= 3


def test_seeds_idempotent(seeded_rls_db):
    from django.db import connection

    from tests.conftest import SEED_MIGRATIONS, _sql_from_migration

    def counts():
        return (
            SensorMetricType.objects.count(),
            SystemConfiguration.objects.count(),
            TierQuotaMapping.objects.count(),
            Province.objects.count(),
        )

    before = counts()
    # Re-run every seed payload — ON CONFLICT DO NOTHING must make it a no-op.
    with connection.cursor() as cursor:
        for path in SEED_MIGRATIONS:
            cursor.execute(_sql_from_migration(path))
    assert counts() == before
