"""
Tenants — the organization root of the tenancy tree (database.md Compartment 1).

فارسی: ریشهٔ چند-اجاره‌ای بودن سیستم؛ هر ردیف داده‌ای در کل پلتفرم به همین
سازمان وصل است و RLS روی ستون org_id ساخته می‌شود.

Verbatim rules (versions/v0.1/TASKS.md standing constraints): db_table keeps
the DOC's name (RLS scripts + psql walkthrough speak it); nullable stays
nullable; SERIAL stays AutoField (int4); no "improvements".
"""

import uuid

from django.db import models
from django.db.models.functions import Now


class Organization(models.Model):
    """Compartment 1: the tenant root. Suspension = is_active=False, never
    deletion (doc intro); the offboarding flow lives in org_offboarding_log."""

    org_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    subscription_tier = models.CharField(max_length=50, default="basic", null=True)
    billing_email = models.CharField(max_length=255, null=True)
    is_active = models.BooleanField(default=True, null=True)
    created_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "organizations"
