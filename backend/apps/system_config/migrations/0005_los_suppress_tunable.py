"""Seed: the LOS-restart suppression tunable (v0.4 M4-07).

Value VERBATIM from database.md / DR Plan T3 — D21: workers never hardcode
these. Without the seed the watchdog would fall back to a code default and
the fleet-wide false-LOS protection would be unconfigurable.
فارسی: کلیدِ سکوتِ هشدارِ از دست رفتنِ داده پس از ری‌استارتِ ردیس — عیناً طبق سند.
"""

from django.db import migrations

SQL = """
INSERT INTO system_configuration (key, value, description) VALUES
    ('los_suppress_after_redis_restart_min', '10', 'Minutes to suppress the LOS watchdog after a Redis restart (DR T3) — avoids a fleet-wide false LOS wave while sensor:status:* keys repopulate. AU-fix 2026-08-16.')
ON CONFLICT (key) DO NOTHING;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("system_config", "0004_alert_tunables"),
        ("tenants", "0002_rls_policies"),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=migrations.RunSQL.noop),
    ]