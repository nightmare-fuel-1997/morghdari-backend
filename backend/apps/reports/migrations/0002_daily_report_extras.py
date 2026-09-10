"""reports 0002 — daily_report_extras (v0.7 M7-16, §4s R-02 merge).

The ONLY DDL of the reports-merge decision: one additive 1:1 side table,
byte-verbatim from docs/api-reports-merge-v1.md §6. Zero ALTER on the frozen
daily_reports (BN-02 saloon_profile pattern). Rows are written lazily —
canonical-only reports never get one.

RLS: the same tenant_isolation FORCE template (the doc's comment on org_id).

Guarded to vendor=='postgresql' (M7-08/M7-15 pattern); model UNMANAGED.

فارسی: جدولِ ضمیمهٔ گزارش روزانه — فقط افزودنی، بدونِ هیچ ALTER روی جدولِ
یخ‌زده؛ همان قالبِ FORCE-RLS.
"""

from django.db import connection, migrations

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import models


def _sql() -> str:
    if connection.vendor == "sqlite":
        return "SELECT 1;"
    return """
DO $$
BEGIN
  IF to_regclass('public.daily_report_extras') IS NULL THEN
    CREATE TABLE public.daily_report_extras (
      report_id          INT PRIMARY KEY REFERENCES public.daily_reports(id) ON DELETE CASCADE,
      org_id             UUID NOT NULL REFERENCES public.organizations(org_id) ON DELETE RESTRICT,
      bird_count         INT,
      avg_temperature_c  NUMERIC(5,2),
      avg_humidity_pct   NUMERIC(5,2),
      ventilation_status VARCHAR(20) CHECK (ventilation_status IN ('STABLE', 'NEEDS_REVIEW', 'PROBLEM')),
      health_status      VARCHAR(20) CHECK (health_status IN ('NORMAL', 'WARNING', 'CRITICAL')),
      checklist_feed     BOOLEAN NOT NULL DEFAULT FALSE,
      checklist_water    BOOLEAN NOT NULL DEFAULT FALSE,
      checklist_device   BOOLEAN NOT NULL DEFAULT FALSE,
      created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX idx_report_extras_org ON public.daily_report_extras(org_id);
  END IF;
END
$$;

ALTER TABLE public.daily_report_extras ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_report_extras FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON daily_report_extras;
CREATE POLICY tenant_isolation ON daily_report_extras
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0001_initial"),
    ]

    operations = [
        # State registration for the UNMANAGED model (compat/0001 pattern —
        # same as reports/0001): records DailyReportExtras in the migration
        # graph; Django emits no SQL for it, the DDL is the RunSQL below.
        migrations.CreateModel(
            name='DailyReportExtras',
            fields=[
                ('report', models.OneToOneField(db_column='report_id', on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='extras', serialize=False, to='reports.dailyreport')),
                ('bird_count', models.IntegerField(blank=True, null=True)),
                ('avg_temperature_c', models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ('avg_humidity_pct', models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ('ventilation_status', models.CharField(blank=True, max_length=20, null=True)),
                ('health_status', models.CharField(blank=True, max_length=20, null=True)),
                ('checklist_feed', models.BooleanField(default=False)),
                ('checklist_water', models.BooleanField(default=False)),
                ('checklist_device', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
            ],
            options={
                'db_table': 'daily_report_extras',
                'managed': False,
            },
        ),
        migrations.RunSQL(
            sql=_sql(),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
