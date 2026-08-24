"""
TimescaleDB DDL for the unmanaged telemetry tables (database.md Compartment 3).

فارسی: تمام DDLِ جدول‌های hypertable اینجا اجرا می‌شود چون Django آن‌ها را
نمی‌سازد (managed=False) — ساخت جدول، ایندکس‌ها، فشرده‌سازی و retention.

Everything here is verbatim from database.md Compartment 3:
- CREATE TABLE for the two unmanaged tables (Django's CreateModel ops above
  are state-only and emit no SQL for managed=False).
- create_hypertable + add_dimension(org_id, 4) — org space-partition.
- idx_telemetry_buffered partial index: crucial for the Celery retroactive
  alert sweep (v0.4).
- tenant_isolation RLS on both hypertables (Compartment 8 mandate).
- Retention (180 d) policy enforces the §5 Storage Strategy drop date;
  compression is BLOCKED by C-11 (mutually exclusive with RLS) pending the
  owner decision in Open Items Tracker §7.1.
  metric_hourly_aggregates is deliberately NOT retained away — permanent
  summaries (AU-fix 2026-08-16).

Guarded to vendor=='postgresql': sqlite laptop runs skip silently; CI/dev/prod
all run the timescale image (C-04 pin). The leading CREATE EXTENSION IF NOT
EXISTS makes migrate self-healing on volumes bootstrapped before initdb/
00_extensions.sql existed (defensive ops; fresh volumes already have it).
"""

from django.db import migrations

SQL = """
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ===================== saloon_telemetry (raw hypertable) =====================
CREATE TABLE IF NOT EXISTS saloon_telemetry (
    server_received_ts  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    device_ts           TIMESTAMPTZ NOT NULL,
    org_id              UUID NOT NULL REFERENCES organizations(org_id),
    saloon_id           INT NOT NULL REFERENCES saloons(id),
    slot_id             VARCHAR(50) NOT NULL REFERENCES saloon_sensor_slots(slot_id),

    is_buffered         BOOLEAN DEFAULT FALSE,
    buffered_delay_mins NUMERIC(10,2) GENERATED ALWAYS AS ((EXTRACT(EPOCH FROM (server_received_ts - device_ts))/60)::NUMERIC(10,2)) STORED,

    metrics             JSONB NOT NULL DEFAULT '{}',

    -- Partition integrity ONLY, not dedup (Audit Gap-Idem / Saas Decision 14):
    -- partition column server_received_ts must appear in every UNIQUE index;
    -- real dedup is the Redis idempotency guard on (org_id, slot_id, device_ts).
    UNIQUE (server_received_ts, slot_id, org_id),

    clock_drift         BOOLEAN DEFAULT FALSE
);

SELECT create_hypertable('saloon_telemetry', 'server_received_ts');
SELECT add_dimension('saloon_telemetry', 'org_id', number_partitions => 4);

-- Crucial partial index for the Celery retroactive alert sweep.
CREATE INDEX idx_telemetry_buffered ON saloon_telemetry (saloon_id, device_ts DESC) WHERE is_buffered = TRUE;

-- CONSTRAINTS C-11 / Tracker §7.1: TimescaleDB 2.29 refuses compression AND
-- row-level security on the same hypertable in EITHER order. Compartment 8
-- mandates tenant_isolation on every org-scoped table, so the security-first
-- interim state is RLS ON / columnstore OFF; RETENTION below still runs.
-- Owner decision pending in the docs repo before compression may return.
ALTER TABLE saloon_telemetry ENABLE ROW LEVEL SECURITY;
ALTER TABLE saloon_telemetry FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON saloon_telemetry;
CREATE POLICY tenant_isolation ON saloon_telemetry
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID);

-- Lifecycle enforcement (§5 Storage Strategy): drop raw chunks after 180 d.
SELECT add_retention_policy('saloon_telemetry', INTERVAL '180 days');

-- ================= metric_hourly_aggregates (permanent) ======================
CREATE TABLE IF NOT EXISTS metric_hourly_aggregates (
    id           BIGSERIAL,
    bucket_hr    TIMESTAMPTZ NOT NULL,
    org_id       UUID NOT NULL,
    saloon_id    INT NOT NULL,
    slot_id      VARCHAR(50) NOT NULL,
    metric_key   VARCHAR(20) NOT NULL REFERENCES sensor_metric_types(metric_key),
    avg_val      NUMERIC(10,4),
    min_val      NUMERIC(10,4),
    max_val      NUMERIC(10,4),
    stddev_val   NUMERIC(10,4),
    sample_count INT NOT NULL DEFAULT 0,
    high_variance BOOLEAN GENERATED ALWAYS AS (stddev_val > 0.15 * ABS(avg_val)) STORED,
    UNIQUE (bucket_hr, slot_id, metric_key)
);

SELECT create_hypertable('metric_hourly_aggregates', 'bucket_hr');

-- Same C-11 ordering rule (no compression here, but kept symmetric so both
-- hypertables get their RLS from THIS migration, never from 02_rls.sql).
ALTER TABLE metric_hourly_aggregates ENABLE ROW LEVEL SECURITY;
ALTER TABLE metric_hourly_aggregates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON metric_hourly_aggregates;
CREATE POLICY tenant_isolation ON metric_hourly_aggregates
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID);

-- Chart queries: "give me temp for slot X last 7 days".
CREATE INDEX idx_agg_lookup ON metric_hourly_aggregates (slot_id, metric_key, bucket_hr DESC);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("telemetry", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=migrations.RunSQL.noop),
    ]
