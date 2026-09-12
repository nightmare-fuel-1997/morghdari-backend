"""org_daily_usage — per-tenant cost metering rows (v0.7 M7-08, MET-SAAS).

فارسی: جدولِ اندازه‌گیریِ مصرفِ روزانهٔ هر سازمان؛ پایهٔ محاسبهٔ هزینهٔ
هر مستأجر (MET-SAAS) — و لنگرِ سهمیه‌بندیِ فازِ ۲ (billing).

Runbook §5.4 (closed 2026-08-23): "per-org attribution from data already
collected — org:{org_id}:meter:current_hour (messages/day), per-org bytes in
saloon_telemetry chunks, and fleet-level container cost divided by message
share. Exported as tenant_cost_estimate_usd{org_id} by a DAILY CELERY JOB
from v0.7."

Table shape is VERBATIM from docs/data-aggregates-v1.md (the aggregates
contract row):
    org_daily_usage | UNIQUE(org_id, day) |
    msg_count BIGINT, stored_bytes BIGINT, saloon_count INT, gateway_count INT
with the rollup task `aggregates.build_org_usage` reading "stream counters +
pg_total_relation_size samples" at the nightly 01:10 slot.

Owner-approved placement (2026-09-09): apps/monitoring — the writer is the
monitoring daily job, not one of the data-aggregates v0.9 endpoints. The
monitoring app's migrations directory was still empty, so this is 0001 there.

RLS: the doc's FORCE-RLS POLICY TEMPLATE, with the canonical
tenant_isolation equality policy (USING org_id = NULLIF(current_setting(
'app.current_org_id', true), '')::UUID; WITH CHECK org_id = current_setting(
...)::UUID) — identical expression to tenants/0002_rls_policies.py, copied
here because that migration's DO loop only covers tables that existed then.
FORCE marks it fail-closed for table owners too.

Guarded to vendor=='postgresql' like tenants/0003: sqlite laptop runs skip
silently; CI/dev/prod all run Postgres. The model is UNMANAGED so pytest's
--nomigrations gate never tries to CREATE TABLE on sqlite.
"""

import django.db.models
from django.db import connection, migrations


def _sql() -> str:
    if connection.vendor == "sqlite":
        # Laptop quick loop: no RLS on sqlite (same pragmatism as
        # tenants.context / TenantOrgMiddleware).
        return "SELECT 1;"
    return """
DO $$
BEGIN
  IF to_regclass('public.org_daily_usage') IS NULL THEN
    CREATE TABLE public.org_daily_usage (
      id          BIGSERIAL PRIMARY KEY,
      org_id      UUID        NOT NULL REFERENCES public.organizations(org_id),
      day         DATE        NOT NULL,
      msg_count   BIGINT      NOT NULL DEFAULT 0,
      stored_bytes BIGINT     NOT NULL DEFAULT 0,
      saloon_count INTEGER     NOT NULL DEFAULT 0,
      gateway_count INTEGER   NOT NULL DEFAULT 0,
      CONSTRAINT org_daily_usage_org_day UNIQUE (org_id, day)
    );
  END IF;
END
$$;

ALTER TABLE public.org_daily_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.org_daily_usage FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON org_daily_usage;
CREATE POLICY tenant_isolation ON org_daily_usage
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("monitoring", "__first__"),
        ("tenants", "0002_rls_policies"),
    ]

    operations = [
        # State registration for the UNMANAGED model (compat/0001 pattern):
        # records OrgDailyUsage in the migration graph so `makemigrations
        # --check` (migrate-gate step 1) stays clean — the DDL itself comes
        # from the RunSQL below and Django emits no SQL for an unmanaged
        # CreateModel. Without this the gate reds with "model in project but
        # not in migration state". Added 2026-09-10.
        migrations.CreateModel(
            name="OrgDailyUsage",
            fields=[
                ("id", django.db.models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("day", django.db.models.DateField()),
                ("msg_count", django.db.models.BigIntegerField(default=0)),
                ("stored_bytes", django.db.models.BigIntegerField(default=0)),
                ("saloon_count", django.db.models.IntegerField(default=0)),
                ("gateway_count", django.db.models.IntegerField(default=0)),
            ],
            options={
                "db_table": "org_daily_usage",
                "managed": False,
            },
        ),
        migrations.RunSQL(
            sql=_sql(),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
