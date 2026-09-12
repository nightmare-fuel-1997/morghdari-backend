"""
RLS policies applied through Django's migration machinery (TASKS M1-06).

فارسی: همان سیاست‌های 02_rls.sql از راه مهاجرت جنگو — برای محیط‌هایی که
initdb ندارند (پایگاه‌دادهٔ مدیریت‌شده). جدول‌های hypertable استثنا هستند و
سیاست‌شان از مهاجرت تلمتری می‌آید (CONSTRAINTS C-11).

Why duplicated from initdb/02_rls.sql instead of reading it at runtime:
migrations are FROZEN HISTORY — they must replay identically on any future
checkout, so the SQL is inlined verbatim. Drift between the two copies (and
against database.md itself) is enforced by scripts/check_rls_parity.py,
which fails loudly if any of the three sources diverges.

Dependencies pin this AFTER every schema-creating migration, so running
`manage.py migrate` alone yields the fully-isolated schema on a managed
postgres without any initdb volume.
"""

from django.db import migrations

SQL = """
DO $$
DECLARE
  t text;
  tenant_tables text[] := ARRAY[
    'organizations',
    'org_members',
    'account_role_scopes',
    'farms',
    'saloons',
    'gateway_requests',
    'gateway_slots',
    'gateway_hardware_registry',
    'gateway_saloon_zones',
    'saloon_positions',
    'saloon_sensor_slots',
    'hardware_registry',
    'gateway_sensor_mapping',
    'saloon_photos',
    'saloon_actuator_slots',
    'actuator_hardware_registry',
    'flock_cycles',
    'lifecycle_templates',
    'alert_instances',
    'alert_acknowledgements',  -- C-10 resolution: gains org_id, joins the policy universe
    'saloon_operational_states',
    'doctor_tickets',
    'mortality_records',
    'medical_prescriptions',
    'flock_weight_samples',
    'flock_feed_intake',
    'flock_water_intake',
    'biosecurity_events',
    'maintenance_mutes',
    'dashboard_configs',
    'provisioning_audit_log',
    'org_offboarding_log',
    'command_logs',
    'account_activity_logs'
  ];
BEGIN
  FOREACH t IN ARRAY tenant_tables LOOP
    IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
      EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
      EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I '
        'USING (org_id = NULLIF(current_setting(''app.current_org_id'', true), '''')::UUID) '
        'WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::UUID)',
        t
      );
    END IF;
  END LOOP;
END
$$;
"""

# The two hypertables whose RLS lives in telemetry.0002 (C-11) — parity
# script adds these to the migration-side expectation set.
HYPERTABLES_RLS_IN_TELEMETRY = {"saloon_telemetry", "metric_hourly_aggregates"}


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        # alerts/0004, not 0003: the policy universe includes
        # alert_acknowledgements, whose org_id column (C-10) only exists after
        # alerts/0004. Pinned at 0003, a scoped `migrate reports` (via
        # reports/0001 -> tenants/0002) applied the policy before the column
        # existed — UndefinedColumn. Bumped 2026-09-10; SQL unchanged.
        ('alerts', '0004_alertacknowledgement_org'),
        ('auditing', '0001_initial'),
        ('devices', '0001_initial'),
        ('farms', '0001_initial'),
        ('geography', '0001_initial'),
        ('mqtt_auth', '0001_initial'),
        ('provisioning', '0001_initial'),
        ('system_config', '0002_tierquotamapping'),
        ('telemetry', '0002_timescale_hypertables'),
        ('tenants', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=migrations.RunSQL.noop),
    ]
