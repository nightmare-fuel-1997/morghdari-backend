"""
Seed: system_configuration (26 canonical keys) + tier_quota_mapping (3 tiers)
— VERBATIM values and descriptions from database.md Compartment 4.

فارسی: بذرِ پیکربندی و سهمیه‌ها عیناً از داکیومنت. ورکرها هرگز این اعداد را
هاردکد نمی‌کنند — Audit §Gap همین بود.

Idempotent via ON CONFLICT DO NOTHING on the primary keys.
"""

from django.db import migrations

SQL = """
INSERT INTO system_configuration (key, value, description) VALUES
    ('replay_rate_limit_per_min',    '50',  'Max messages/min from historical replay queue'),
    ('org_offboard_grace_days',      '30',  'Days a suspended org is retained before offboard/delete is permitted; export must complete first (Audit LC-4).'),
    ('replay_rate_limit_per_org_per_min', '20', 'Per-ORG cap on historical replay throughput so one tenant''s replay storm cannot monopolize the shared worker budget (noisy-neighbor fairness). Must be <= replay_rate_limit_per_min.'),
    ('quota_exempt_tier1_metrics',   'true', 'If true, Tier-1 safety metrics (temp, nh3) bypass the hourly quota drop in §15.3 — telemetry is ALWAYS ingested for life-safety metrics even when an org is over quota; overage is billed/flagged, never dropped.'),
    ('dedup_window_sec',             '90000', 'TTL for the Redis ingest idempotency guard dedup:{org}:{slot}:{device_ts}. AU-fix 2026-08-16: was 7200, violating its own stated invariant — it must be >= session_expiry_interval (24h = 86400s) or QoS-1 redeliveries after a long subscriber-tier outage double-ingest. 90000 = 25h. Audit Gap-Idem.'),
    ('los_watchdog_interval_sec',    '60', 'How often the LOS watchdog sweep runs'),
    ('clock_drift_alert_threshold_min', '5', 'Minutes of drift before clock alert fires'),
    ('threshold_transition_grace_sec',  '300', 'Grace period on midnight threshold transition'),
    ('cmd_timeout_ttl_sec',          '10',  'Seconds before an unacknowledged command is marked FAILED'),
    ('nh3_max_ts_velocity_threshold', '50', 'nh3.max_ts >= this value triggers velocity emergency bypass'),
    ('telemetry_insert_batch_size',    '50', 'Rows accumulated before bulk_create flush'),
    ('subscriber_mqueue_len',          '20000', 'EMQX max_mqueue_len for subscriber persistent session'),
    ('hysteresis_buffer_units',        '0.5', 'Units below threshold before alert resolves (flapping mitigation)'),
    ('active_flock_temp_tolerance_c',  '2.0', 'Max °C deviation from pre-heat target to allow ACTIVE_FLOCK transition'),
    ('dlq_alert_threshold',           '100', 'Dead letter queue depth that triggers an alert to Hardware Team'),
    ('alert_acknowledgement_pause_minutes', '10', 'Minutes the escalation timer pauses after a Tier 1 alert acknowledgement (Tracker: M5)'),
    ('ws_max_connections_per_user',    '10', 'Maximum concurrent WebSocket connections allowed per user (Tracker: M6)'),
    ('mqtt_cert_renewal_interval_days','90', 'Interval in days for MQTT TLS certificate renewal (Tracker: F8)'),
    ('max_pending_gateway_requests',  '5',  'Maximum pending gateway requests per organization (basic tier); enforced on gateway_requests table via model-level validation. Configurable per tier.'),
    ('los_suppress_after_redis_restart_min', '10', 'Minutes to suppress the LOS watchdog after a Redis restart (DR T3) — avoids a fleet-wide false LOS wave while sensor:status:* keys repopulate. AU-fix 2026-08-16.'),
    ('db_connections_alert_pct', '95', 'PostgreSQL connection-saturation alert threshold (%) — read by the Observability DB-connection alert rule. R3-fix 2026-08-16 (previously referenced in Saas.md with no seeded key).'),
    ('temp_velocity_delta_60s', '5.0', 'Temperature rate-of-change (°C within one 60s window) that qualifies as a velocity emergency. 2026-08-23: replaces the hardcoded 5.0 in the worker — Decision 21 applies to velocity thresholds too (Tracker §4i).'),
    ('metric_autodiscover_min_windows', '3', 'Distinct hourly buckets a previously-unknown metric key must appear in for the same slot before the ingestion worker inserts its sensor_metric_types row; blocks typo keys from polluting the catalog. Keys are ALWAYS stored in JSONB regardless (Tracker §4i).'),
    ('farm_power_outage_los_ratio', '0.7', 'Fraction of a farm''s active sensor slots whose LOS keys must expire within farm_power_outage_window_min to fire ONE consolidated farm power-outage notification (GS-CORRELATION, Tracker §4i).'),
    ('farm_power_outage_window_min', '3', 'Sliding window (minutes) for the GS-CORRELATION simultaneous-LOS detection.'),
    ('correlation_suppress_minutes', '10', 'Minutes of per-farm paging suppression after a consolidated farm power-outage notification — individual LOS alerts still log/alert-instance but do not re-page (GS-CORRELATION, Tracker §4i).')
ON CONFLICT (key) DO NOTHING;

INSERT INTO tier_quota_mapping (tier, max_farms, max_saloons, max_sensor_slots, max_hourly_messages, max_gateways) VALUES
    ('basic',   5,  20, 50,  10000, 2),
    ('pro',     20, 100, 250, 50000, 10),
    ('enterprise', 9999, 9999, 9999, 999999, 9999)
ON CONFLICT (tier) DO NOTHING;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("system_config", "0002_tierquotamapping"),
        ("tenants", "0002_rls_policies"),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=migrations.RunSQL.noop),
    ]
