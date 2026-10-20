"""
Provisioning — audit + offboarding logs (database.md Compartment 4 tail).

فارسی: لاگ ممیزیِ پراوژنینگ (چه‌کسی چه‌وضعیتی را کی عوض کرد) و لاگ خروج
سازمان — تنها آرتیفکتی که بعد از حذف کامل تنانت باقی می‌ماند.
"""

import uuid

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


class InstallationJob(models.Model):
    """One physical gateway installation (AD-16, owner 2026-10-18).

    WHY not a column on gateway_requests: a request is for a FARM, and a farm
    legitimately needs several gateways (Saas.md — a gateway owns a zone of
    saloons, not the whole farm). Each gateway is a separate physical unit,
    bound at a separate moment, possibly by a different installer. Assignment
    therefore has to be per-gateway.

    AD-21: `kind` also covers repair and maintenance visits, so those never need
    a second table. Only INSTALL is implemented in Phase 2.

    State machine: PENDING → ASSIGNED → BOUND → SUBMITTED → COMPLETED, plus
    CANCELLED. `PROVISIONED` on the slot is reached by the reconciler when
    applied == desired — never by a human (AD-17).
    """

    KIND_INSTALL = "INSTALL"
    KIND_REPAIR = "REPAIR"
    KIND_MAINTENANCE = "MAINTENANCE"
    KINDS = [KIND_INSTALL, KIND_REPAIR, KIND_MAINTENANCE]

    STATE_PENDING = "PENDING"
    STATE_ASSIGNED = "ASSIGNED"
    STATE_BOUND = "BOUND"
    STATE_SUBMITTED = "SUBMITTED"
    STATE_COMPLETED = "COMPLETED"
    STATE_CANCELLED = "CANCELLED"
    STATES = [STATE_PENDING, STATE_ASSIGNED, STATE_BOUND, STATE_SUBMITTED,
              STATE_COMPLETED, STATE_CANCELLED]

    id = models.AutoField(primary_key=True)
    kind = models.CharField(max_length=20, default=KIND_INSTALL)
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    farm = models.ForeignKey("farms.Farm", on_delete=models.RESTRICT, related_name="+")
    # Nullable: a REPAIR/MAINTENANCE job has no originating request.
    request = models.ForeignKey(
        "devices.GatewayRequest", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    assigned_installer = models.ForeignKey(
        "accounts.User", db_column="assigned_installer", on_delete=models.RESTRICT,
        null=True, blank=True, related_name="+",
    )
    assigned_by = models.ForeignKey(
        "accounts.User", db_column="assigned_by", on_delete=models.RESTRICT,
        null=True, blank=True, related_name="+",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=20, default=STATE_PENDING)
    # One-to-one so a physical unit can never sit on two jobs at once.
    unit = models.OneToOneField(
        "devices.DeviceUnit", db_column="unit_id", on_delete=models.RESTRICT,
        null=True, blank=True, related_name="+",
    )
    gateway_slot = models.OneToOneField(
        "devices.GatewaySlot", db_column="gateway_slot_id", on_delete=models.RESTRICT,
        null=True, blank=True, related_name="+",
    )
    # AD-21: reassignment is allowed after bind; this makes it visible in the
    # job history instead of silently rewriting the assignee.
    reassigned_after_bind = models.BooleanField(default=False)
    created_at = models.DateTimeField(null=True, db_default=Now())
    completed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "installation_jobs"
        constraints = [
            # Literal lists, not the class constants: a nested Meta cannot see
            # the enclosing class namespace.
            models.CheckConstraint(
                check=Q(kind__in=["INSTALL", "REPAIR", "MAINTENANCE"]),
                name="ck_installation_jobs_kind",
            ),
            models.CheckConstraint(
                check=Q(state__in=["PENDING", "ASSIGNED", "BOUND", "SUBMITTED", "COMPLETED", "CANCELLED"]),
                name="ck_installation_jobs_state",
            ),
        ]
        indexes = [
            # The installer queue: "my jobs in this state", newest first.
            models.Index(fields=["assigned_installer", "state"], name="idx_jobs_assignee_state"),
            models.Index(fields=["org", "state"], name="idx_jobs_org_state"),
        ]


class GatewayCommand(models.Model):
    """Command journal for device management (AD-5, 2026-10-18).

    WHY not `command_logs`: that table is saloon-scoped (saloon_id NOT NULL),
    actuator-only (CHECK heater/exhaust_fan/curtain), has no attempt counter or
    expiry, and carries legal_hold retention semantics. Gateway-level
    provisioning has no saloon and needs PENDING/EXPIRED/SUPERSEDED states.
    The Blueprint §11.2 state MATRIX is reused verbatim; only the storage
    changes.

    Three distinct events, never collapsed (design doc §5.3):
      DB row created  → state PENDING
      MQTT PUBACK     → state SENT      (sent_at)
      device reports  → state APPLIED   (applied_at) / FAILED
    """

    KIND_PROVISION = "PROVISION"
    KIND_ROTATE = "ROTATE_CREDENTIAL"
    KIND_DIAGNOSTICS = "DIAGNOSTICS_REQUEST"
    KIND_REBOOT = "REBOOT"
    KIND_REFRESH = "REFRESH_CONFIG"
    KINDS = [KIND_PROVISION, KIND_ROTATE, KIND_DIAGNOSTICS, KIND_REBOOT, KIND_REFRESH]

    STATE_PENDING = "PENDING"
    STATE_SENT = "SENT"
    STATE_ACKNOWLEDGED = "ACKNOWLEDGED"
    STATE_APPLIED = "APPLIED"
    STATE_FAILED = "FAILED"
    STATE_EXPIRED = "EXPIRED"
    STATE_SUPERSEDED = "SUPERSEDED"
    STATES = [STATE_PENDING, STATE_SENT, STATE_ACKNOWLEDGED, STATE_APPLIED,
              STATE_FAILED, STATE_EXPIRED, STATE_SUPERSEDED]

    command_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    gateway_slot = models.ForeignKey("devices.GatewaySlot", on_delete=models.RESTRICT, related_name="+")
    job = models.ForeignKey(
        InstallationJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    kind = models.CharField(max_length=30)
    payload = models.JSONField(null=True, blank=True)
    # For PROVISION rows: the config_version this command carries, so the
    # reconciler can correlate a report with the exact document sent.
    config_version = models.PositiveIntegerField(null=True, blank=True)
    state = models.CharField(max_length=20, default=STATE_PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", db_column="created_by", on_delete=models.RESTRICT,
        null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(null=True, db_default=Now())
    sent_at = models.DateTimeField(null=True, blank=True)
    acked_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "gateway_commands"
        constraints = [
            models.CheckConstraint(
                check=Q(kind__in=[
                    "PROVISION", "ROTATE_CREDENTIAL", "DIAGNOSTICS_REQUEST",
                    "REBOOT", "REFRESH_CONFIG",
                ]),
                name="ck_gateway_commands_kind",
            ),
            models.CheckConstraint(
                check=Q(state__in=[
                    "PENDING", "SENT", "ACKNOWLEDGED", "APPLIED",
                    "FAILED", "EXPIRED", "SUPERSEDED",
                ]),
                name="ck_gateway_commands_state",
            ),
        ]
        indexes = [
            models.Index(fields=["gateway_slot", "-created_at"], name="idx_gw_cmd_slot_time"),
            # Drives the expiry sweep and the publish-retry sweep.
            models.Index(
                fields=["state"],
                name="idx_gw_cmd_open",
                condition=Q(state__in=["PENDING", "SENT", "ACKNOWLEDGED"]),
            ),
        ]
