"""Batch-1 daily aggregates — v0.9 M9-10 (docs/data-aggregates-v1.md §2).

Four permanent daily-grain tables (read-accelerators with compute-fallback —
no read path is rewired here): metric_daily_aggregates, flock_daily_snapshots,
alert_daily_stats, gateway_daily_uptime. Shapes verbatim from the spec §2;
every table carries org scoping + the canonical tenant_isolation policy.

Same construction as monitoring/0001 (org_daily_usage): UNMANAGED models with
CreateModel STATE registration (makemigrations --check stays clean) + RunSQL
DDL guarded to postgresql (sqlite laptop runs skip silently; CI/dev/prod run
Postgres). Reverse is noop — aggregates rebuild from source, never migrate
data backwards.
"""

import django.db.models
from django.db import connection, migrations
from django.db.models import RESTRICT


def _sql() -> str:
    if connection.vendor == "sqlite":
        return "SELECT 1;"
    tables = {
        "metric_daily_aggregates": """
      id          BIGSERIAL PRIMARY KEY,
      org_id      UUID        NOT NULL REFERENCES public.organizations(org_id),
      saloon_id   INTEGER     NOT NULL,
      slot_id     VARCHAR(50) NOT NULL,
      metric_key  VARCHAR(50) NOT NULL,
      day         DATE        NOT NULL,
      avg_val     NUMERIC(10, 4) NULL,
      min_val     NUMERIC(10, 4) NULL,
      max_val     NUMERIC(10, 4) NULL,
      inband_minutes INTEGER  NOT NULL DEFAULT 0,
      sample_count  INTEGER   NOT NULL DEFAULT 0,
      CONSTRAINT metric_daily_aggregates_grain
        UNIQUE (org_id, saloon_id, slot_id, metric_key, day)""",
        "flock_daily_snapshots": """
      id          BIGSERIAL PRIMARY KEY,
      org_id      UUID        NOT NULL REFERENCES public.organizations(org_id),
      flock_id    INTEGER     NOT NULL,
      saloon_id   INTEGER     NOT NULL,
      day         DATE        NOT NULL,
      mortality_cum INTEGER   NOT NULL DEFAULT 0,
      mortality_pct NUMERIC(5, 2) NULL,
      feed_cum_kg NUMERIC(10, 2) NULL,
      water_cum_l NUMERIC(10, 2) NULL,
      avg_weight_g NUMERIC(8, 2) NULL,
      fcr_to_date NUMERIC(10, 3) NULL,
      CONSTRAINT flock_daily_snapshots_grain
        UNIQUE (flock_id, saloon_id, day)""",
        "alert_daily_stats": """
      id          BIGSERIAL PRIMARY KEY,
      org_id      UUID        NOT NULL REFERENCES public.organizations(org_id),
      saloon_id   INTEGER     NOT NULL,
      day         DATE        NOT NULL,
      tier        INTEGER     NOT NULL,
      metric_key  VARCHAR(20) NOT NULL,
      alert_count INTEGER     NOT NULL DEFAULT 0,
      mttr_min    NUMERIC(10, 2) NULL,
      ack_to_resolve_min NUMERIC(10, 2) NULL,
      CONSTRAINT alert_daily_stats_grain
        UNIQUE (org_id, saloon_id, day, tier, metric_key)""",
        "gateway_daily_uptime": """
      id          BIGSERIAL PRIMARY KEY,
      gateway_slot_id VARCHAR(50) NOT NULL,
      org_id      UUID        NOT NULL REFERENCES public.organizations(org_id),
      day         DATE        NOT NULL,
      online_pct  NUMERIC(5, 2) NULL,
      los_minutes INTEGER     NOT NULL DEFAULT 0,
      buffered_pct NUMERIC(5, 2) NULL,
      CONSTRAINT gateway_daily_uptime_grain
        UNIQUE (gateway_slot_id, org_id, day)""",
    }
    parts = []
    for name, cols in tables.items():
        parts.append(f"""
DO $$
BEGIN
  IF to_regclass('public.{name}') IS NULL THEN
    CREATE TABLE public.{name} ({cols}
    );
  END IF;
END
$$;

ALTER TABLE public.{name} ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.{name} FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON {name};
CREATE POLICY tenant_isolation ON {name}
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
    WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID);
""")
    return "\n".join(parts)


def _state(name, fields, table):
    return migrations.CreateModel(
        name=name,
        fields=[
            ("id", django.db.models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            *fields,
        ],
        options={"db_table": table, "managed": False},
    )


def _org_fk():
    return ("org", django.db.models.ForeignKey(
        db_column="org_id", on_delete=RESTRICT, related_name="+", to="tenants.organization"))


def _saloon_fk():
    return ("saloon", django.db.models.ForeignKey(
        db_column="saloon_id", on_delete=RESTRICT, related_name="+", to="farms.saloon"))


class Migration(migrations.Migration):

    dependencies = [
        ("monitoring", "0001_orgdailyusage"),
        ("tenants", "0002_rls_policies"),
    ]

    operations = [
        _state("MetricDailyAggregate", [
            _org_fk(),
            _saloon_fk(),
            ("slot_id", django.db.models.CharField(max_length=50)),
            ("metric_key", django.db.models.CharField(max_length=50)),
            ("day", django.db.models.DateField()),
            ("avg_val", django.db.models.DecimalField(max_digits=10, decimal_places=4, null=True)),
            ("min_val", django.db.models.DecimalField(max_digits=10, decimal_places=4, null=True)),
            ("max_val", django.db.models.DecimalField(max_digits=10, decimal_places=4, null=True)),
            ("inband_minutes", django.db.models.IntegerField(default=0)),
            ("sample_count", django.db.models.IntegerField(default=0)),
        ], "metric_daily_aggregates"),
        _state("FlockDailySnapshot", [
            _org_fk(),
            ("flock", django.db.models.ForeignKey(
                db_column="flock_id", on_delete=RESTRICT, related_name="+", to="telemetry.flockcycle")),
            _saloon_fk(),
            ("day", django.db.models.DateField()),
            ("mortality_cum", django.db.models.IntegerField(default=0)),
            ("mortality_pct", django.db.models.DecimalField(max_digits=5, decimal_places=2, null=True)),
            ("feed_cum_kg", django.db.models.DecimalField(max_digits=10, decimal_places=2, null=True)),
            ("water_cum_l", django.db.models.DecimalField(max_digits=10, decimal_places=2, null=True)),
            ("avg_weight_g", django.db.models.DecimalField(max_digits=8, decimal_places=2, null=True)),
            ("fcr_to_date", django.db.models.DecimalField(max_digits=10, decimal_places=3, null=True)),
        ], "flock_daily_snapshots"),
        _state("AlertDailyStat", [
            _org_fk(),
            _saloon_fk(),
            ("day", django.db.models.DateField()),
            ("tier", django.db.models.IntegerField()),
            ("metric_key", django.db.models.CharField(max_length=20)),
            ("alert_count", django.db.models.IntegerField(default=0)),
            ("mttr_min", django.db.models.DecimalField(max_digits=10, decimal_places=2, null=True)),
            ("ack_to_resolve_min", django.db.models.DecimalField(max_digits=10, decimal_places=2, null=True)),
        ], "alert_daily_stats"),
        _state("GatewayDailyUptime", [
            ("gateway_slot_id", django.db.models.CharField(max_length=50)),
            _org_fk(),
            ("day", django.db.models.DateField()),
            ("online_pct", django.db.models.DecimalField(max_digits=5, decimal_places=2, null=True)),
            ("los_minutes", django.db.models.IntegerField(default=0)),
            ("buffered_pct", django.db.models.DecimalField(max_digits=5, decimal_places=2, null=True)),
        ], "gateway_daily_uptime"),
        migrations.RunSQL(
            sql=_sql(),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
