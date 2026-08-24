"""
Auditing — command logs + account activity (database.md Compartments 5–6).

فارسی: لاگ فرمان‌های اکچوئیتور و لاگ فعالیت کاربران. ستون legal_hold در
command_logs جلوی پاک‌شدن ردیف‌ها توسط purge/archive را می‌گیرد (DR §5).
"""

import uuid

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now


class CommandLog(models.Model):
    """Every actuator command with full lifecycle status.

    2026-08-23 (Tracker §4i): legal_hold honored by purge/archive (DR Plan §5).
    """

    command_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    farm = models.ForeignKey("farms.Farm", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, related_name="+")
    actuator_type = models.CharField(max_length=30)
    action_value = models.CharField(max_length=50)
    params = models.JSONField(null=True, blank=True)
    current_status = models.CharField(max_length=20, default="SENT")
    # 2026-08-23 (Tracker §4i): held rows are exported but never purged until cleared.
    legal_hold = models.BooleanField(default=False)
    actor = models.ForeignKey("accounts.User", db_column="actor_id", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(null=True, db_default=Now())
    updated_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "command_logs"
        constraints = [
            models.CheckConstraint(
                check=Q(actuator_type__in=["heater", "exhaust_fan", "curtain"]),
                name="ck_command_logs_actuator_type",
            ),
            models.CheckConstraint(
                check=Q(current_status__in=["SENT", "ACKNOWLEDGED", "SUCCESS", "FAILED"]),
                name="ck_command_logs_current_status",
            ),
        ]
        indexes = [
            models.Index(
                fields=["current_status"],
                name="idx_cmd_pending",
                condition=Q(current_status__in=["SENT", "ACKNOWLEDGED"]),
            ),
        ]


class AccountActivityLog(models.Model):
    """Immutable audit trail; details JSONB carries e.g.
    {"old_value": 20, "new_value": 200, "key": "nh3_max"}."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(db_default=Now())
    actor = models.ForeignKey("accounts.User", db_column="actor_id", on_delete=models.RESTRICT, related_name="+")
    action = models.CharField(max_length=100)
    ip_address = models.GenericIPAddressField(null=True, blank=True)  # INET on postgres
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    farm = models.ForeignKey("farms.Farm", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    details = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "account_activity_logs"
        indexes = [
            models.Index(F("org"), F("farm"), F("saloon"), F("timestamp").desc(), name="idx_logs_hierarchy"),
        ]
