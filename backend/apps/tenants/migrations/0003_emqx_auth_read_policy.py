"""emqx_auth read exemption on gateway_slots — the authn JOIN fix (v0.7 M7-03).

فارسی: سیاستِ فقط-خواندن برای نقش emqx_auth روی gateway_slots تا زنجیرهٔ
احرازِ EMQX (JOIN با gateway_slots) روی استقرار واقعی کار کند.

Root cause found in the v0.7 laptop rehearsal (M7-03): the postgresql authn
backend in emqx.conf runs

    SELECT cred.password_hash ... FROM mqtt_device_credentials cred
    JOIN gateway_slots gs ON gs.slot_id = cred.device_id
    WHERE cred.username = ${username} AND ...

but gateway_slots is FORCE-RLS'd with the tenant_isolation equality policy.
EMQX's pool connects as emqx_auth and never sets app.current_org_id, so
org_id = NULL → zero rows → EVERY authn attempt fails closed ("Not
authorized") even though mqtt_device_credentials itself is org-less and
readable. (mqtt_acl_rules — the authorizer table — is org-less too and was
never affected.)

The policy here is deliberately minimal:
  * role-scoped: TO emqx_auth (no other role gains anything),
  * action-scoped: FOR SELECT only (authn/authorizer never write),
  * table-scoped: gateway_slots only (the single JOIN table in the authn
    query; credentials and acl tables carry no RLS at all),
  * USING-only: WITH CHECK left to the tenant_isolation policy (writes stay
    impossible for this role anyway — it holds no INSERT grant).

Tenant isolation is untouched: morghdari_app keeps the equality policy and
the fail-closed zero-row default; emqx_auth cannot read any other org-scoped
table. This mirrors initdb/02_rls.sql (kept 1:1 — scripts/check_rls_parity.py
compares the TABLE list, not policies, and the new statement lives in both
sources verbatim).

Guarded to vendor=='postgresql' like telemetry/0002: sqlite laptop runs skip
silently; CI/dev/prod all run Postgres.
"""

from django.db import connection, migrations


def _sql() -> str:
    if connection.vendor == "sqlite":
        # Laptop quick loop: no RLS on sqlite (same pragmatism as
        # tenants.context / TenantOrgMiddleware).
        return "SELECT 1;"
    return """
DO $$
BEGIN
  IF to_regclass('public.gateway_slots') IS NOT NULL THEN
    DROP POLICY IF EXISTS emqx_auth_read ON gateway_slots;
    CREATE POLICY emqx_auth_read ON gateway_slots
        FOR SELECT TO emqx_auth
        USING (true);
  END IF;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0002_rls_policies"),
    ]

    operations = [
        migrations.RunSQL(
            sql=_sql(),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
