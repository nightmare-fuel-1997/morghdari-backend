"""v1.0 billing placeholder — `manage.py billing_stub` (2026-10-28).

The command must (a) aggregate org_daily_usage per org for a month,
(b) bill saloons/gateways pro-rata by saloon-days / days-in-month,
(c) default every price to 0 so an unconfigured system produces an honest
zero invoice, and (d) emit JSON suitable for handing to an accountant.
"""
import json
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.core.management.base import CommandError

from apps.monitoring.management.commands.billing_stub import _month_bounds, _price
from apps.monitoring.models import OrgDailyUsage
from apps.system_config.models import SystemConfiguration
from apps.tenants.models import Organization

pytestmark = pytest.mark.django_db

# org_daily_usage is an UNMANAGED table created by a Postgres-only RunSQL
# migration (Timescale/RLS era). The local SQLite quick-loop cannot create it,
# so the DB-backed assertions run on Postgres (CI gate / rehearsal) only.
pg_only = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="org_daily_usage is a Postgres-only unmanaged table",
)


@pytest.fixture(autouse=True)
def _ensure_usage_table():
    """Materialise the unmanaged table on a FRESH test DB.

    `--nomigrations` means RunSQL-only tables never get created, and
    `--reuse-db` used to hide that (an old DB happened to have the table).
    Same posture as conftest's migration-artifact materialisation: create it
    here when absent, Postgres only.
    """
    if connection.vendor != "postgresql":
        yield
        return
    with connection.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.org_daily_usage (
              id           BIGSERIAL PRIMARY KEY,
              org_id       UUID   NOT NULL,
              day          DATE   NOT NULL,
              msg_count    BIGINT NOT NULL DEFAULT 0,
              stored_bytes BIGINT NOT NULL DEFAULT 0,
              saloon_count INTEGER NOT NULL DEFAULT 0,
              gateway_count INTEGER NOT NULL DEFAULT 0,
              UNIQUE (org_id, day)
            )
        """)
    yield


def _org(name="Billing Test Org"):
    return Organization.objects.create(name=name)


def _usage(org, day, msgs, saloons=1, gateways=1, bytes_=1024):
    OrgDailyUsage.objects.create(
        org=org, day=day, msg_count=msgs, stored_bytes=bytes_,
        saloon_count=saloons, gateway_count=gateways,
    )


def test_month_bounds_and_bad_month():
    first, last = _month_bounds("2026-02")
    assert first == date(2026, 2, 1) and last == date(2026, 2, 28)
    with pytest.raises(CommandError):
        _month_bounds("2026-13-x")


@pg_only
def test_unconfigured_pricing_bills_zero_but_counts_usage():
    org = _org()
    _usage(org, date(2026, 10, 1), msgs=1000)
    _usage(org, date(2026, 10, 2), msgs=500)

    out = StringIO()
    call_command("billing_stub", "--month", "2026-10", "--json", stdout=out)
    payload = json.loads(out.getvalue())
    inv = next(i for i in payload["invoices"] if i["org_id"] == str(org.org_id))
    assert inv["messages"] == 1500
    assert inv["total"] == 0.0            # placeholder: no prices configured
    assert inv["pricing_configured"] is False


@pg_only
def test_priced_invoice_is_prorated():
    org = _org("Priced Org")
    _usage(org, date(2026, 10, 1), msgs=10, saloons=2, gateways=1)
    _usage(org, date(2026, 10, 2), msgs=10, saloons=2, gateways=1)
    SystemConfiguration.objects.create(key="billing_base_monthly", value="100")
    SystemConfiguration.objects.create(key="billing_price_per_saloon", value="10")
    SystemConfiguration.objects.create(key="billing_price_per_gateway", value="5")

    # 2 days of 31 → saloon_months = 2*2/31, gateway_months = 2*1/31
    assert _price("billing_price_per_saloon") == Decimal("10")
    out = StringIO()
    call_command("billing_stub", "--month", "2026-10", "--json", stdout=out)
    inv = next(i for i in json.loads(out.getvalue())["invoices"]
               if i["org_id"] == str(org.org_id))
    assert inv["pricing_configured"] is True
    expected = 100 + 10 * (Decimal(4) / 31) + 5 * (Decimal(2) / 31)
    assert abs(inv["total"] - float(round(expected, 2))) < 0.02


@pg_only
def test_org_filter_and_empty_month():
    a, b = _org("Org A"), _org("Org B")
    _usage(a, date(2026, 9, 5), msgs=7)
    _usage(b, date(2026, 9, 5), msgs=9)

    out = StringIO()
    call_command("billing_stub", "--month", "2026-09", "--org", str(a.org_id),
                 "--json", stdout=out)
    rows = json.loads(out.getvalue())["invoices"]
    assert [r["org_id"] for r in rows] == [str(a.org_id)]

    out = StringIO()
    call_command("billing_stub", "--month", "2026-01", stdout=out)
    assert "no metered usage" in out.getvalue()
