"""
Provisioning — audit + offboarding logs (database.md Compartment 4 tail).

فارسی: لاگ ممیزیِ پراوژنینگ (چه‌کسی چه‌وضعیتی را کی عوض کرد) و لاگ خروج
سازمان — تنها آرتیفکتی که بعد از حذف کامل تنانت باقی می‌ماند.
"""

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now


class ProvisioningAuditLog(models.Model):
    """Non-repudiation for provisioning: every change to
    gateway_slots.provisioning_status lands here (who/when/what changed)."""

    gateway_slot = models.ForeignKey("devices.GatewaySlot", db_column="gateway_slot_id", on_delete=models.RESTRICT, related_name="+")
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    previous_status = models.CharField(max_length=30, null=True, blank=True)
    new_status = models.CharField(max_length=30)
    changed_by = models.ForeignKey("accounts.User", on_delete=models.RESTRICT, related_name="+")
    changed_at = models.DateTimeField(null=True, db_default=Now())
    reason = models.CharField(max_length=50, null=True, blank=True)
    details = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "provisioning_audit_log"
        indexes = [
            models.Index(F("gateway_slot"), F("changed_at").desc(), name="idx_prov_audit_slot"),
        ]


class OrgOffboardingLog(models.Model):
    """The ONE artifact that survives a tenant delete (Audit LC-4): per-table
    deleted row counts + export checksum so the platform can attest
    "org X was exported on D1 and erased on D2". Deliberately NOT FK'd to
    organizations — that row is gone; bare UUID for the historical record."""

    org_id = models.UUIDField()  # bare UUID; organizations row is gone
    org_name = models.CharField(max_length=200, null=True, blank=True)  # snapshot for the human record
    action = models.CharField(max_length=20)
    export_checksum = models.CharField(max_length=128, null=True, blank=True)  # SHA-256 of delivered archive
    deleted_counts = models.JSONField(null=True, blank=True)  # {"saloon_telemetry": 123456, ...}
    performed_by = models.ForeignKey("accounts.User", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    performed_at = models.DateTimeField(null=True, db_default=Now())
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "org_offboarding_log"
        constraints = [
            models.CheckConstraint(
                check=Q(action__in=["SUSPENDED", "EXPORTED", "DELETED"]),
                name="ck_org_offboarding_log_action",
            ),
        ]
        indexes = [
            models.Index(F("org_id"), F("performed_at").desc(), name="idx_offboard_org"),
        ]
