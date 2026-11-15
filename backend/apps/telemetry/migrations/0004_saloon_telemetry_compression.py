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
DROP POLICY IF EXISTS tenant_isolation ON saloon_telemetry;
ALTER TABLE saloon_telemetry NO FORCE ROW LEVEL SECURITY;
ALTER TABLE saloon_telemetry DISABLE ROW LEVEL SECURITY;

ALTER TABLE saloon_telemetry SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'server_received_ts DESC',
    timescaledb.compress_segmentby = 'org_id, saloon_id'
);

SELECT add_compression_policy('saloon_telemetry', INTERVAL '90 days', if_not_exists => TRUE);
"""


def _apply_compression(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.saloon_telemetry') IS NOT NULL")
        if not cursor.fetchone()[0]:
            return
        for stmt in (s.strip() for s in SQL.split(";") if s.strip()):
            cursor.execute(stmt)


class Migration(migrations.Migration):

    dependencies = [
        ("telemetry", "0003_seed_metric_types"),
    ]

    operations = [
        migrations.RunPython(_apply_compression, reverse_code=migrations.RunPython.noop),
    ]
