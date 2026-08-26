"""Seed: four alert-engine tunables (v0.4 M4-01, Tracker §4l Q1).

Values VERBATIM from System Walkthrough (breach 900s, tiers 900/1800s marks)
and Saas.md sensor-status TTL — D21: workers never hardcode these.
فارسی: چهار کلیدِ تنظیمیِ موتور هشدار عیناً طبق سند بذر می‌شوند.
"""

from django.db import migrations

SQL = """
INSERT INTO system_configuration (key, value, description) VALUES
    ('breach_duration_sec',      '900', 'Seconds a metric must stay in breach before an alert fires (Walkthrough: duration filter).'),
    ('sensor_status_ttl_sec',    '300', 'TTL for sensor:status:{saloon_id}:{slot_id} keys written on every ingested reading; expiry feeds the LOS watchdog.'),
    ('escalation_tier2_min',     '15', 'Minutes an alert stays Tier 1 before escalating to Tier 2.'),
    ('escalation_tier3_min',     '30', 'Minutes an alert stays unacknowledged before escalating to Tier 3.')
ON CONFLICT (key) DO NOTHING;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("system_config", "0003_seed_configuration_and_quotas"),
        ("tenants", "0002_rls_policies"),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=migrations.RunSQL.noop),
    ]
