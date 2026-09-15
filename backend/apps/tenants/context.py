"""
Tenant context — the ONLY sanctioned way to set the RLS GUC.

فارسی: تنها راهِ مجاز برای تنظیم زمینهٔ سازمان؛ پارامتری و محدود به تراکنش تا
هیچ‌گاه بین کانکشن‌های استخر نشتی نکند (Compartment 8 قاعدهٔ GUC lifecycle).

GUC lifecycle rule (database.md Compartment 8): `app.current_org_id` is set
ONLY via parameterized `set_config(..., true)` — the `true` makes it
transaction-scoped, so it dies with COMMIT/ROLLBACK and cannot leak across
pooled connections. Never f-string the value; never use `set_config(...,
false)` (session scope = pool poisoning).

Usage (subscriber/Celery paths; HTTP middleware lands in v0.2):

    from django.db import transaction
    from apps.tenants.context import tenant_context

    with transaction.atomic(), tenant_context(org_id):
        ...  # every query here is RLS-scoped to org_id

Outside such a block the app role sees ZERO rows by design (fail-closed).
"""

from contextlib import contextmanager
from uuid import UUID

from django.db import connection


@contextmanager
def tenant_context(org_id: UUID | str):
    """Set app.current_org_id for the remainder of the CURRENT transaction."""
    if connection.vendor == "sqlite":
        # Same pragmatism as TenantOrgMiddleware: sqlite (laptop quick loop)
        # has no set_config and no RLS to feed — the GUC would be dead weight.
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_org_id', %s, true)", [str(org_id)])
    yield


@contextmanager
def user_context(user_id):
    """Set app.current_user_id for the remainder of the CURRENT transaction.

    The login-time twin of tenant_context(): before a user has org claims
    there is no org to scope to, yet the membership resolver must still see
    the caller's OWN org_members row (backed by the additive
    org_members_self_lookup SELECT policy — tenants/0004). Same lifecycle
    rules as tenant_context: parameterized, tx-scoped, dies with COMMIT —
    never session scope, never f-stringed.
    """
    if connection.vendor == "sqlite":
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_user_id', %s, true)", [str(user_id)])
    yield


def admin_alias() -> str:
    """The sanctioned RLS-bypassing DB alias for CROSS-ORG discovery.

    BUG-9 / BUG-11 family (QA 8.4b, 12.7, 20.x): the Celery worker and the
    anonymous device endpoint connect as the app role, and every tenant table
    is FORCE ROW LEVEL SECURITY - a context-free read returns ZERO rows by
    design (fail-closed). Background sweeps and the device gateway-config
    lookup must therefore DISCOVER their rows over the admin alias (the same
    posture replay.py / thresholds recache / rebuild_state / monitoring rollup
    already use), while every per-tenant read+write still runs inside
    tenant_context on the default alias. Falls back to "default" where no
    admin DSN is configured (laptop sqlite quick loop and test settings).

    فارسی: alias ادمین فقط برای «کشفِ بینسازمانی» است؛ خواندن/نوشتنِ هر
    سازمان همچنان داخل tenant_context روی alias پیشفرض انجام میشود. جایی که
    admin پیکربندی نشده باشد به alias پیشفرض برمیگردد تا RLS روی sqlite
    بیاثر بماند.
    """
    from django.db import connections

    return "admin" if "admin" in connections.databases else "default"
