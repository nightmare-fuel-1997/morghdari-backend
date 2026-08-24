"""
System configuration — every tunable constant in ONE table (database.md
Compartment 4; ⚠️ Audit §Gap fix).

فارسی: تمام ثابت‌های قابل‌تنظیم سیستم اینجا متمرکز شده‌اند؛ ورکرها هیچ مقداری را
هاردکد نمی‌کنند. کلیدها در M1-07 عیناً از داکیومنت seed می‌شوند.

Org-less global: shared platform-wide, NO org_id, NO RLS policy.
"""

from django.db import models
from django.db.models.functions import Now


class SystemConfiguration(models.Model):
    key = models.CharField(max_length=100, primary_key=True)
    value = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, db_default=Now())
    updated_by = models.ForeignKey("accounts.User", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "system_configuration"


class TierQuotaMapping(models.Model):
    """Per-tier resource quotas (database.md Compartment 4 tail). Enforcement
    is at the DJANGO MODEL LAYER (save-signal count checks + 403) and the
    midnight cron re-syncs counts to Redis — deliberately NOT a DB CHECK so a
    tier change (Phase 2 Stripe webhook) lifts limits without migrations.
    Org-less global: NO org_id, NO RLS policy. Seed rows land in M1-07."""

    tier = models.CharField(max_length=50, primary_key=True)
    max_farms = models.IntegerField(default=5)
    max_saloons = models.IntegerField(default=20)
    max_sensor_slots = models.IntegerField(default=50)
    max_hourly_messages = models.IntegerField(default=10000)
    max_gateways = models.IntegerField(default=2)

    class Meta:
        db_table = "tier_quota_mapping"
