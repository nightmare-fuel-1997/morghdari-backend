"""v1.7.1 — the built-in (org NULL) template catalog becomes tenant-readable.

WHY (2026-09-12, uncovered while building the template write surface): the
`tenant_isolation` policy compares `org_id = current_setting('app.current_org_id')`,
and a NULL org_id never equals anything — so under ENFORCED RLS (rehearsal,
prod, every `SET ROLE morghdari_app` test) the org-NULL built-in catalog was
INVISIBLE while dev/CI (superuser) saw it plainly. That latent split was
harmless while templates were seeded-only and nobody read O4 under RLS. It stops
being harmless now: the resolution chain's last step is the global default, and
a manager's catalog screen (O4) must be able to show the built-in rows.

The exemption is deliberately minimal, in the same shape as its two siblings
(0003 EMQX authn read, 0004 org_members self-lookup):

  * SELECT only — writes stay governed by tenant_isolation, so no tenant can
    create, edit or overwrite a built-in row through this policy;
  * rows with org_id IS NULL only — tenant-owned rows of OTHER orgs stay
    invisible (the isolation guarantee is untouched);
  * TO morghdari_app only — the app role; every other role keeps its own set.

initdb/02_rls.sql carries the identical block for the environments bootstrap
creates before Django migrates. scripts/check_rls_parity.py is unaffected: it
compares the ARRAY[*] TABLE SET, and this adds a policy, not a table.

فارسی: کاتالوگِ داخلی (org = NULL) برای نقشِ برنامه فقط-خواندنی می‌شود؛ ردیف‌های
سازمان‌های دیگر همچنان نامرئی می‌مانند و مسیرِ نوشتن دست‌نخورده است.
"""

from django.db import migrations

SQL = """
DO $$
BEGIN
  IF to_regclass('public.lifecycle_templates') IS NOT NULL THEN
    DROP POLICY IF EXISTS lifecycle_templates_global_read ON lifecycle_templates;
    CREATE POLICY lifecycle_templates_global_read ON lifecycle_templates
        FOR SELECT TO morghdari_app
        USING (org_id IS NULL);
  END IF;
END
$$;
"""

REVERSE_SQL = (
    "DROP POLICY IF EXISTS lifecycle_templates_global_read ON lifecycle_templates;"
)


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0005_lifecycle_template_write_surface"),
        ("tenants", "0004_orgmember_self_lookup"),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=REVERSE_SQL),
    ]
