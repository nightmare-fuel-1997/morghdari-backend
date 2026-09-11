"""org_members self-lookup — the login-under-RLS fix (2026-09-11 rehearsal finding).

فارسی: سیاستِ فقط-خواندن برای ردیفِ عضویتِ خودِ کاربر تا لاگین روی استقرارِ
واقعی (FORCE RLS + نقش morghdari_app) کار کند.

Root cause found live on the v0.7 rehearsal stack: POST /api/v1/auth/login/
validated the password, then died at resolve_active_membership() with 401
"no active org membership". org_members is FORCE-RLS'd with the
tenant_isolation equality policy (org_id = current_setting('app.current_org_id')),
and a login/refresh request carries NO org claim yet — the GUC is unset, so
every row is invisible to morghdari_app. Dev/CI never saw it: they connect as
superuser/test roles (RLS bypassed) or sqlite (no RLS).

The policy here is deliberately minimal, mirroring 0003_emqx_auth_read:
  * action-scoped: FOR SELECT only — writes stay governed by tenant_isolation
    (WITH CHECK untouched, so a user can never INSERT/UPDATE/DELETE a
    membership via the self branch; see the policy-combination note below),
  * row-scoped: USING ("user" = current app.current_user_id) — a session can
    read ONLY its own membership rows, never another user's,
  * GUC-sourced: app.current_user_id is set by resolve_active_membership
    (tenants/context.user_context) from the ALREADY-AUTHENTICATED principal —
    never from client input, parameterized + tx-scoped like the org GUC.

Policy combination semantics: permissive policies OR together per action.
SELECT sees tenant_isolation OR self_lookup; INSERT/UPDATE/DELETE see only
tenant_isolation (self_lookup grants nothing there), so escalation via
self-INSERT or cross-org row smuggling is impossible.

Callers: apps/accounts/serializers.resolve_active_membership wraps its reads
in transaction.atomic() + user_context(user.id), then tenant_context(org) for
the role-scope/org reads — the login and refresh claim paths both.

Tenant isolation is otherwise untouched. This mirrors initdb/02_rls.sql (kept
1:1 per the 0003 convention; scripts/check_rls_parity.py compares the TABLE
list, not policies).

Guarded to vendor=='postgresql' like 0003: sqlite laptop runs skip silently;
CI/dev/prod all run Postgres.
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
  IF to_regclass('public.org_members') IS NOT NULL THEN
    DROP POLICY IF EXISTS org_members_self_lookup ON org_members;
    CREATE POLICY org_members_self_lookup ON org_members
        FOR SELECT TO morghdari_app
        USING ("user" = NULLIF(current_setting('app.current_user_id', true), '')::integer);
  END IF;
END
$$;
"""


def _reverse_sql() -> str:
    if connection.vendor == "sqlite":
        return "SELECT 1;"
    return """
DO $$
BEGIN
  IF to_regclass('public.org_members') IS NOT NULL THEN
    DROP POLICY IF EXISTS org_members_self_lookup ON org_members;
  END IF;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0003_emqx_auth_read_policy"),
    ]

    operations = [
        migrations.RunSQL(
            sql=_sql(),
            reverse_sql=_reverse_sql(),
        ),
    ]
