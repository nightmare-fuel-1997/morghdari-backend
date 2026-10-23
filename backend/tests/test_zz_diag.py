import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


def test_diag_tables_exist():
    with connection.cursor() as c:
        for t in ("legacy_slug_map", "metric_daily_aggregates", "daily_reports"):
            c.execute("SELECT to_regclass(%s) IS NOT NULL", [f"public.{t}"])
            print(f"DIAG {t} exists={c.fetchone()[0]}")
