"""Phase 0 (2026-10-18) — RLS + broker grant for the MQTT-native provisioning tables.

WHY a NEW migration instead of extending tenants/0002: 0002 is FROZEN HISTORY —
it must replay byte-identically on any future checkout, and its docstring says
so. Adding table names to it would rewrite what already ran on production.
This migration applies the SAME `tenant_isolation` policy template to the three
org-scoped tables Phase 0 introduces, and grants the broker's read-only role
access to the one org-less table it now needs.

Tables and why each is (or is not) enrolled:

  * installation_jobs  — org-scoped. One physical gateway installation (AD-16).
  * gateway_commands   — org-scoped. The device-management command journal (AD-5).
  * device_photos      — org-scoped. Per-gateway / per-sensor install photos (AD-20).
  * device_units       — **deliberately ABSENT**: the factory inventory is
    org-less by design (AD-1), exactly like mqtt_device_credentials and
    mqtt_acl_rules, which carry no policy because the broker reads them. RLS
    would be meaningless (no org_id column) and access control is the
    platform-staff gate on the endpoints.

The grant is the other half of AD-1: the EMQX authn query gains a LEFT JOIN on
device_units so a factory-registered, not-yet-bound unit can authenticate.
Without SELECT the broker sees an empty join and every unbound unit fails at
CONNECT. initdb/01_roles.sql carries the identical block for environments
bootstrap creates before Django migrates.

فارسی: سه جدولِ سازمان‌دارِ فاز صفر زیر سیاستِ جداسازیِ تِنانت می‌روند؛ جدولِ
انبارِ کارخانه (device_units) عمداً بدون RLS است چون org_id ندارد و مانند
mqtt_device_credentials فقط با نقشِ خواندنیِ بروکر خوانده می‌شود.
"""

from django.db import migrations

FORWARD = """
DO $$
DECLARE
  t text;
  tenant_tables text[] := ARRAY[
    'installation_jobs',
    'gateway_commands',
    'device_photos'
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

DO $$
BEGIN
  IF to_regclass('public.device_units') IS NOT NULL THEN
    GRANT SELECT ON device_units TO emqx_auth;
  END IF;
END
$$;
"""

REVERSE = """
DO $$
DECLARE
  t text;
  tenant_tables text[] := ARRAY[
    'installation_jobs',
    'gateway_commands',
    'device_photos'
  ];
BEGIN
  FOREACH t IN ARRAY tenant_tables LOOP
    IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
      EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
      EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
      EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
    END IF;
  END LOOP;
END
$$;

DO $$
BEGIN
  IF to_regclass('public.device_units') IS NOT NULL THEN
    REVOKE SELECT ON device_units FROM emqx_auth;
  END IF;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        # The tables must exist first. Both new app migrations depend on
        # tenants/0005 (not on this one), so there is no cycle.
        ("tenants", "0005_lifecycle_templates_global_read"),
        ("devices", "0002_phase0_devices_provisioning"),
        ("provisioning", "0002_phase0_devices_provisioning"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
    ]
