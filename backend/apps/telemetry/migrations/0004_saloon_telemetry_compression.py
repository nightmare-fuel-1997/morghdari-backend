"""
Converge existing databases onto C-11 Approach B for saloon_telemetry:
disable PostgreSQL RLS on saloon_telemetry and enable TimescaleDB columnstore
compression + 90-day compression policy (metric_hourly_aggregates keeps FORCE RLS).

فارسی: همگراسازی پایگاه‌داده‌های موجود به رویکرد B در C-11 — غیرفعال‌سازی RLS روی
saloon_telemetry و فعال‌سازی فشرده‌سازی ستونی TimescaleDB (در حالی که
metric_hourly_aggregates همچنان FORCE RLS دارد).
"""

from django.db import migrations

SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class
        WHERE relname = 'saloon_telemetry' AND (relrowsecurity OR relforcerowsecurity)
    ) THEN
        DROP POLICY IF EXISTS tenant_isolation ON saloon_telemetry;
        ALTER TABLE saloon_telemetry NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE saloon_telemetry DISABLE ROW LEVEL SECURITY;
    END IF;

    IF EXISTS (
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'saloon_telemetry' AND NOT compression_enabled
    ) THEN
        ALTER TABLE saloon_telemetry SET (
            timescaledb.compress,
            timescaledb.compress_orderby = 'server_received_ts DESC',
            timescaledb.compress_segmentby = 'org_id, saloon_id'
        );
    END IF;

    PERFORM add_compression_policy('saloon_telemetry', INTERVAL '90 days', if_not_exists => TRUE);
    PERFORM add_retention_policy('saloon_telemetry', INTERVAL '180 days', if_not_exists => TRUE);
END
$$;
"""


def _apply_compression(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.saloon_telemetry') IS NOT NULL")
        if not cursor.fetchone()[0]:
            return
        cursor.execute(SQL)


class Migration(migrations.Migration):

    dependencies = [
        ("telemetry", "0003_seed_metric_types"),
    ]

    operations = [
        migrations.RunPython(_apply_compression, reverse_code=migrations.RunPython.noop),
    ]
