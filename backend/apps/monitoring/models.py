"""OrgDailyUsage — per-tenant cost metering row (v0.7 M7-08, MET-SAAS).

Table shape verbatim from docs/data-aggregates-v1.md; DDL + FORCE-RLS policy
live in monitoring/0001 (UNMANAGED here — same pattern as SaloonTelemetry).
فارسی: ردیفِ مصرفِ روزانهٔ هر سازمان؛ مدلِ فقط-خواندن، DDL در مهاجرتِ ۰۰۰۱.
"""

from django.db import models


class OrgDailyUsage(models.Model):
    id = models.BigAutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    day = models.DateField()
    msg_count = models.BigIntegerField(default=0)
    stored_bytes = models.BigIntegerField(default=0)
    saloon_count = models.IntegerField(default=0)
    gateway_count = models.IntegerField(default=0)

    class Meta:
        db_table = "org_daily_usage"
        managed = False
        constraints = [
            models.UniqueConstraint(fields=["org", "day"], name="org_daily_usage_org_day"),
        ]


# ====================================================================
# BATCH 1 DAILY AGGREGATES (v0.9 M9-10, docs/data-aggregates-v1.md §2).
# Read-accelerators with compute-fallback (§5): writers only, no read-path
# rewiring here — KPI/compliance/quality keep per-request computation until
# a written Tracker decision removes the fallbacks.
# All UNMANAGED (DDL + FORCE-RLS in monitoring/0002, same pattern as
# OrgDailyUsage/monitoring-0001): permanent daily grain, Tehran-calendar day.
# ====================================================================


class MetricDailyAggregate(models.Model):
    id = models.BigAutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", db_column="saloon_id", on_delete=models.RESTRICT, related_name="+")
    slot_id = models.CharField(max_length=50)
    metric_key = models.CharField(max_length=50)
    day = models.DateField()
    avg_val = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    min_val = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    max_val = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    inband_minutes = models.IntegerField(default=0)
    sample_count = models.IntegerField(default=0)

    class Meta:
        db_table = "metric_daily_aggregates"
        managed = False
        constraints = [
            models.UniqueConstraint(
                fields=["org", "saloon", "slot_id", "metric_key", "day"],
                name="metric_daily_aggregates_grain",
            ),
        ]


class FlockDailySnapshot(models.Model):
    id = models.BigAutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    flock = models.ForeignKey("telemetry.FlockCycle", db_column="flock_id", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", db_column="saloon_id", on_delete=models.RESTRICT, related_name="+")
    day = models.DateField()
    mortality_cum = models.IntegerField(default=0)
    mortality_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    feed_cum_kg = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    water_cum_l = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    avg_weight_g = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    fcr_to_date = models.DecimalField(max_digits=10, decimal_places=3, null=True)

    class Meta:
        db_table = "flock_daily_snapshots"
        managed = False
        constraints = [
            models.UniqueConstraint(
                fields=["flock", "saloon", "day"],
                name="flock_daily_snapshots_grain",
            ),
        ]


class AlertDailyStat(models.Model):
    id = models.BigAutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", db_column="saloon_id", on_delete=models.RESTRICT, related_name="+")
    day = models.DateField()
    tier = models.IntegerField()
    metric_key = models.CharField(max_length=20)
    alert_count = models.IntegerField(default=0)
    mttr_min = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    ack_to_resolve_min = models.DecimalField(max_digits=10, decimal_places=2, null=True)

    class Meta:
        db_table = "alert_daily_stats"
        managed = False
        constraints = [
            models.UniqueConstraint(
                fields=["org", "saloon", "day", "tier", "metric_key"],
                name="alert_daily_stats_grain",
            ),
        ]


class GatewayDailyUptime(models.Model):
    id = models.BigAutoField(primary_key=True)
    gateway_slot_id = models.CharField(max_length=50)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    day = models.DateField()
    online_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    los_minutes = models.IntegerField(default=0)
    buffered_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True)

    class Meta:
        db_table = "gateway_daily_uptime"
        managed = False
        constraints = [
            models.UniqueConstraint(
                fields=["gateway_slot_id", "org", "day"],
                name="gateway_daily_uptime_grain",
            ),
        ]
