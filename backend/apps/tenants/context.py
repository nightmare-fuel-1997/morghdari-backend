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
