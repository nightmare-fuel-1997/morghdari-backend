"""reports 0001 — the four §4n tables (v0.7 M7-15).

Table shapes are VERBATIM from docs database.md (§4n block, 2026-09-04):
daily_reports, report_medication_usages, vet_medication_catalog,
support_tickets — including the CHECK constraints, the doc's two indexes on
daily_reports, and the partial assignee index on support_tickets.

RLS: the doc's FORCE-RLS POLICY TEMPLATE for all four (tenant_isolation,
Tracker §4n), with the canonical equality policy — identical expression to
tenants/0002_rls_policies.py and monitoring/0001_orgdailyusage.py. FORCE
marks them fail-closed for table owners too.

Guarded to vendor=='postgresql' like tenants/0003 and monitoring/0001: the
sqlite laptop loop skips silently; CI/dev/prod all run Postgres. The models
are UNMANAGED so pytest's --nomigrations gate never tries to CREATE TABLE.

فارسی: مهاجرتِ چهار جدولِ گزارش‌ها — دقیقاً طبق database.md با الگوی
FORCE-RLS و گاردِ وندور (فقط Postgres).
"""

from django.db import connection, migrations

import django.db.models.functions.datetime
from django.db import models


def _sql() -> str:
    if connection.vendor == "sqlite":
        # Laptop quick loop: no RLS on sqlite (same pragmatism as
        # tenants.context / TenantOrgMiddleware).
        return "SELECT 1;"
    return """
DO $$
BEGIN
  IF to_regclass('public.daily_reports') IS NULL THEN
    CREATE TABLE public.daily_reports (
      id              SERIAL PRIMARY KEY,
      org_id          UUID NOT NULL REFERENCES public.organizations(org_id) ON DELETE RESTRICT,
      farm_id         INT  NOT NULL REFERENCES public.farms(id) ON DELETE RESTRICT,
      saloon_id       INT  NOT NULL REFERENCES public.saloons(id) ON DELETE RESTRICT,
      flock_id        INT  NOT NULL REFERENCES public.flock_cycles(flock_id) ON DELETE RESTRICT,
      report_date     DATE NOT NULL,
      mortality_count INT  NOT NULL DEFAULT 0,
      feed_kg         NUMERIC(10,2),
      water_l         NUMERIC(10,2),
      avg_weight_g    NUMERIC(8,2),
      notes           TEXT,
      status          VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
                      CHECK (status IN ('DRAFT', 'SUBMITTED', 'FLAGGED', 'APPROVED', 'REJECTED')),
      submitted_by    INT REFERENCES public.accounts_user(id),
      submitted_at    TIMESTAMPTZ NULL,
      reviewed_by     INT REFERENCES public.accounts_user(id),
      reviewed_at     TIMESTAMPTZ NULL,
      review_note     TEXT,
      vet_approved_by INT REFERENCES public.accounts_user(id),
      vet_approved_at TIMESTAMPTZ NULL,
      created_at      TIMESTAMPTZ DEFAULT NOW(),
      CONSTRAINT uq_daily_reports_flock_saloon_date UNIQUE (flock_id, saloon_id, report_date)
    );
    CREATE INDEX idx_reports_org ON public.daily_reports(org_id, report_date DESC);
    CREATE INDEX idx_reports_saloon ON public.daily_reports(saloon_id, report_date DESC);
  END IF;

  IF to_regclass('public.vet_medication_catalog') IS NULL THEN
    CREATE TABLE public.vet_medication_catalog (
      id             SERIAL PRIMARY KEY,
      org_id         UUID NOT NULL REFERENCES public.organizations(org_id) ON DELETE RESTRICT,
      created_by     INT  NOT NULL REFERENCES public.accounts_user(id),
      name           VARCHAR(200) NOT NULL,
      dosage_default VARCHAR(100),
      unit           VARCHAR(20),
      notes          TEXT,
      is_active      BOOLEAN NOT NULL DEFAULT TRUE,
      created_at     TIMESTAMPTZ DEFAULT NOW(),
      CONSTRAINT uq_vet_medication_catalog_org_name UNIQUE (org_id, name)
    );
    CREATE INDEX idx_vetcat_org ON public.vet_medication_catalog(org_id, is_active);
  END IF;

  IF to_regclass('public.report_medication_usages') IS NULL THEN
    CREATE TABLE public.report_medication_usages (
      id              SERIAL PRIMARY KEY,
      org_id          UUID NOT NULL REFERENCES public.organizations(org_id) ON DELETE RESTRICT,
      report_id       INT  NOT NULL REFERENCES public.daily_reports(id) ON DELETE CASCADE,
      catalog_item_id INT  NULL REFERENCES public.vet_medication_catalog(id) ON DELETE SET NULL,
      medication_name VARCHAR(200) NOT NULL,
      dosage          VARCHAR(100),
      frequency       VARCHAR(100)
    );
    CREATE INDEX idx_medusage_org ON public.report_medication_usages(org_id, report_id);
  END IF;

  IF to_regclass('public.support_tickets') IS NULL THEN
    CREATE TABLE public.support_tickets (
      id               SERIAL PRIMARY KEY,
      org_id           UUID NOT NULL REFERENCES public.organizations(org_id) ON DELETE RESTRICT,
      saloon_id        INT  NULL REFERENCES public.saloons(id) ON DELETE RESTRICT,
      raised_by        INT  NOT NULL REFERENCES public.accounts_user(id),
      assigned_to      INT  NULL REFERENCES public.accounts_user(id),
      category         VARCHAR(20) NOT NULL
                       CHECK (category IN ('medical', 'support', 'installer', 'report')),
      linked_report_id INT  NULL REFERENCES public.daily_reports(id) ON DELETE SET NULL,
      title            VARCHAR(200) NOT NULL,
      description      TEXT,
      status           VARCHAR(20) NOT NULL DEFAULT 'OPEN'
                       CHECK (status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED')),
      priority         VARCHAR(10) DEFAULT 'NORMAL'
                       CHECK (priority IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')),
      created_at       TIMESTAMPTZ DEFAULT NOW(),
      resolved_at      TIMESTAMPTZ NULL,
      resolution_notes TEXT
    );
    CREATE INDEX idx_suptickets_org ON public.support_tickets(org_id, status);
    CREATE INDEX idx_suptickets_assignee
      ON public.support_tickets(assigned_to) WHERE status IN ('OPEN', 'IN_PROGRESS');
  END IF;
END
$$;

ALTER TABLE public.daily_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_reports FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON daily_reports;
CREATE POLICY tenant_isolation ON daily_reports
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID);

ALTER TABLE public.report_medication_usages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.report_medication_usages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON report_medication_usages;
CREATE POLICY tenant_isolation ON report_medication_usages
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID);

ALTER TABLE public.vet_medication_catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.vet_medication_catalog FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON vet_medication_catalog;
CREATE POLICY tenant_isolation ON vet_medication_catalog
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID);

ALTER TABLE public.support_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.support_tickets FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON support_tickets;
CREATE POLICY tenant_isolation ON support_tickets
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "__first__"),
        # tenants.0002 transitively brings accounts/farms/etc.; telemetry is
        # listed EXPLICITLY — flock_cycles (telemetry.0001) is a FK target and
        # tenants.0002 does not depend on the telemetry app.
        ("tenants", "0002_rls_policies"),
        ("telemetry", "0003_seed_metric_types"),
    ]

    operations = [
        # State registration for the UNMANAGED models (compat/0001 pattern):
        # records DailyReport + VetMedicationCatalog + ReportMedicationUsage +
        # SupportTicket in the migration graph so `makemigrations --check`
        # (migrate-gate step 1) stays clean — the DDL itself comes from the
        # RunSQL below and Django emits no SQL for unmanaged CreateModels.
        # Block order mirrors the DDL order (FK targets first). Added 2026-09-10.
        migrations.CreateModel(
            name='DailyReport',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('report_date', models.DateField()),
                ('mortality_count', models.IntegerField(default=0)),
                ('feed_kg', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('water_l', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('avg_weight_g', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True)),
                ('notes', models.TextField(blank=True, null=True)),
                ('status', models.CharField(default='DRAFT', max_length=20)),
                ('submitted_at', models.DateTimeField(blank=True, null=True)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('review_note', models.TextField(blank=True, null=True)),
                ('vet_approved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), null=True)),
            ],
            options={
                'db_table': 'daily_reports',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='VetMedicationCatalog',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=200)),
                ('dosage_default', models.CharField(blank=True, max_length=100, null=True)),
                ('unit', models.CharField(blank=True, max_length=20, null=True)),
                ('notes', models.TextField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), null=True)),
            ],
            options={
                'db_table': 'vet_medication_catalog',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='ReportMedicationUsage',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('medication_name', models.CharField(max_length=200)),
                ('dosage', models.CharField(blank=True, max_length=100, null=True)),
                ('frequency', models.CharField(blank=True, max_length=100, null=True)),
            ],
            options={
                'db_table': 'report_medication_usages',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='SupportTicket',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('category', models.CharField(max_length=20)),
                ('title', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True, null=True)),
                ('status', models.CharField(default='OPEN', max_length=20)),
                ('priority', models.CharField(default='NORMAL', max_length=10)),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), null=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('resolution_notes', models.TextField(blank=True, null=True)),
            ],
            options={
                'db_table': 'support_tickets',
                'managed': False,
            },
        ),
        migrations.RunSQL(
            sql=_sql(),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
