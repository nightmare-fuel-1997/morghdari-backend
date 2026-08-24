"""
M1-08 fail-closed proof: the app role sees NOTHING without tenant context.

فارسی: اثباتِ بسته-به-خطا — نقش اپ بدون GUC هیچ ردیفی نمی‌بیند، با GUC سازمان
خود فقط ردیف‌های همان سازمان را می‌بیند و درجِ بین-سازمانی رد می‌شود.

Postgres-only (needs real RLS); sqlite laptop loops skip. Runs with
transaction=True so the test mirrors REAL usage: middleware/Celery wrap each
unit of work in an atomic block together with tenant_context(), and the
transaction-scoped GUC provably dies at COMMIT/ROLLBACK. The connection runs
as a superuser, so app-role views are emulated via SET ROLE; RESET ROLE in
finally guards against leakage into later tests (CONN_MAX_AGE pooling).
"""

import pytest
from django.db import connection, transaction

from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.context import tenant_context
from apps.tenants.models import Organization

pytestmark = [
    pytest.mark.use_migrations,
    pytest.mark.django_db(transaction=True),
]


def _fetchone(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def _make_org_with_saloon(name: str) -> tuple[Organization, Saloon]:
    province = Province.objects.create(name=f"P-{name}")
    city = City.objects.create(province=province, name=f"C-{name}")
    org = Organization.objects.create(name=name)
    farm = Farm.objects.create(org=org, name=f"F-{name}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"S-{name}")
    return org, saloon


def test_app_role_fail_closed_and_scoped(seeded_rls_db):
    org_a, _saloon_a = _make_org_with_saloon("A")
    org_b, saloon_b = _make_org_with_saloon("B")

    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")
    try:
        # 1. No GUC anywhere ⇒ zero rows (fail-closed default).
        assert _fetchone("SELECT count(*) FROM saloons") == 0

        # 2. Transaction-scoped context for org A ⇒ exactly org A's row.
        with transaction.atomic(), tenant_context(org_a.org_id):
            assert _fetchone("SELECT count(*) FROM saloons") == 1
            assert _fetchone("SELECT name FROM saloons") == "S-A"

            # 3. Cross-org INSERT blocked by WITH CHECK; the violation dies
            # inside its own SAVEPOINT.
            with pytest.raises(Exception):
                with transaction.atomic():
                    with connection.cursor() as c2:
                        c2.execute(
                            "INSERT INTO saloons (farm_id, org_id, name) VALUES (%s, %s, 'evil')",
                            [saloon_b.farm_id, str(org_b.org_id)],
                        )
        # 4. COMMIT ended the transaction ⇒ GUC gone ⇒ fail-closed again.
        # This is the pool-leak guarantee: no next request can inherit A.
        assert _fetchone("SELECT count(*) FROM saloons") == 0

        # 5. Org B context sees only B.
        with transaction.atomic(), tenant_context(org_b.org_id):
            assert _fetchone("SELECT count(*) FROM saloons") == 1
            assert _fetchone("SELECT name FROM saloons") == "S-B"
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_tenant_context_parameterized_never_interpolates(seeded_rls_db):
    """The GUC helper must go through bind params: an injection-shaped value
    must be stored as inert TEXT (custom GUCs accept any string), never parsed
    as SQL — the payload must not execute and the table must survive."""
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")
    try:
        evil = "x'; DROP TABLE organizations; --"
        with transaction.atomic():
            with tenant_context(evil):
                # Policy cast happens on USE, not on SET — a scoped read of a
                # matching-org row errors harmlessly; the point here is that
                # nothing EXECUTED as SQL.
                pass
        assert _fetchone("SELECT to_regclass('public.organizations') IS NOT NULL")
        # Quirk (C-12): after COMMIT a tx-local GUC reads back as EMPTY STRING,
        # not NULL — which is exactly why the policy template must be
        # NULLIF-guarded. Assert the death-of-context behavior itself:
        assert _fetchone("SELECT current_setting('app.current_org_id', true)") == ""
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
