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
