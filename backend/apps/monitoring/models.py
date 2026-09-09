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
