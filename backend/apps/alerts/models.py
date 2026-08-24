"""
Alerts — thresholds, alert lifecycle, operational state, tickets, mutes
(database.md Compartment 4 core; D22/D23 production records land in M1-04).

فارسی: هستهٔ هشدار — قواعد آستانه، چرخهٔ هشدار، وضعیت عملیاتی سالن، تیکت پزشک،
بی‌صداکنندهٔ تعمیرات. همهٔ اعداد قابل‌تنظیم در system_configuration زندگی می‌کنند؛
ورکرها هرگز مقدار را هاردکد نمی‌کنند (Audit §Gap).
"""

import uuid

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now


class LifecycleTemplate(models.Model):
    """Threshold templates. org_id NULL = global/built-in template (doc)."""

    template_id = models.AutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    breed_type = models.CharField(max_length=50, null=True, blank=True)
    name = models.CharField(max_length=100, null=True, blank=True)
    is_default = models.BooleanField(default=False, null=True)

    class Meta:
        db_table = "lifecycle_templates"


class LifecycleThresholdRule(models.Model):
    """Day-range threshold band per metric ('temp','humidity','lux','nh3')."""

    id = models.AutoField(primary_key=True)
    template = models.ForeignKey(LifecycleTemplate, on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    day_from = models.SmallIntegerField()
    day_to = models.SmallIntegerField()
    metric = models.CharField(max_length=20)
    min_value = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    max_value = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "lifecycle_threshold_rules"


class AlertInstance(models.Model):
    """One row per alert lifecycle. current_value enables exact Redis state
    rebuild after data loss (Audit T3); batch_id makes replay sweeps idempotent.
    legal_hold: purge/archive export but never delete until cleared (DR Plan §5)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    farm = models.ForeignKey("farms.Farm", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, related_name="+")
    metric_type = models.CharField(max_length=20)
    # 'ACTIVE_TIER_1/2/3', 'RESOLVED' (free-form per doc — no CHECK constraint).
    current_status = models.CharField(max_length=20)
    current_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=20, default="REALTIME")  # 'REALTIME' | 'REPLAY_ANALYSIS'
    batch_id = models.CharField(max_length=50, null=True, blank=True)
    triggered_at = models.DateTimeField(db_default=Now())
    resolved_at = models.DateTimeField(null=True, blank=True)
    highest_tier_reached = models.IntegerField(default=1, null=True)
    legal_hold = models.BooleanField(default=False)

    class Meta:
        db_table = "alert_instances"
        indexes = [
            models.Index(
                fields=["saloon", "metric_type"],
                name="idx_alert_active",
                condition=Q(resolved_at__isnull=True),
            ),
        ]


class AlertAcknowledgement(models.Model):
    """⚠️ FIX (Audit M5): pause_minutes default is a seed fallback only — the
    runtime value MUST come from system_configuration key
    alert_acknowledgement_pause_minutes."""

    alert = models.ForeignKey(AlertInstance, on_delete=models.RESTRICT, related_name="+")
    acknowledged_by = models.ForeignKey("accounts.User", on_delete=models.RESTRICT, related_name="+")
    acknowledged_at = models.DateTimeField(db_default=Now())
    pause_minutes = models.IntegerField(default=10)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "alert_acknowledgements"
        indexes = [
            models.Index(fields=["alert"], name="idx_ack_alert"),
        ]


class SaloonOperationalState(models.Model):
    """State-machine history: EMPTY_SANITATION → PRE_HEATING → ACTIVE_FLOCK →
    HARVESTED_VACANT."""

    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, related_name="+")
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    state = models.CharField(max_length=30)
    entered_at = models.DateTimeField(db_default=Now())
    exited_at = models.DateTimeField(null=True, blank=True)
    triggered_by = models.ForeignKey("accounts.User", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "saloon_operational_states"
        constraints = [
            models.CheckConstraint(
                check=Q(state__in=["EMPTY_SANITATION", "PRE_HEATING", "ACTIVE_FLOCK", "HARVESTED_VACANT"]),
                name="ck_saloon_operational_states_state",
            ),
        ]
        indexes = [
            models.Index(F("saloon"), F("entered_at").desc(), name="idx_saloon_state"),
        ]


class DoctorTicket(models.Model):
    """Raised by Farm Engineers for medical observations; assigned_to NULL =
    unassigned (auto-assign to first available doctor lands in a later version)."""

    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, related_name="+")
    raised_by = models.ForeignKey("accounts.User", on_delete=models.RESTRICT, related_name="+")
    assigned_to = models.ForeignKey("accounts.User", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, default="OPEN")
    priority = models.CharField(max_length=10, default="NORMAL", null=True)
    created_at = models.DateTimeField(null=True, db_default=Now())
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "doctor_tickets"
        constraints = [
            models.CheckConstraint(
                check=Q(status__in=["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"]),
                name="ck_doctor_tickets_status",
            ),
            models.CheckConstraint(
                check=Q(priority__in=["LOW", "NORMAL", "HIGH", "URGENT"]),
                name="ck_doctor_tickets_priority",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "status"], name="idx_tickets_org"),
            models.Index(
                fields=["assigned_to"],
                name="idx_tickets_assignee",
                condition=Q(status__in=["OPEN", "IN_PROGRESS"]),
            ),
        ]


class MortalityRecord(models.Model):
    """Linked to environmental breaches so Farm Doctors can correlate mortality
    spikes with alert windows."""

    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    flock = models.ForeignKey("telemetry.FlockCycle", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, related_name="+")
    recorded_by = models.ForeignKey("accounts.User", on_delete=models.RESTRICT, related_name="+")
    count = models.IntegerField()
    cause = models.CharField(max_length=100, null=True, blank=True)  # e.g. ammonia_toxicity, heat_stress, unknown, culling
    notes = models.TextField(null=True, blank=True)
    occurred_at = models.DateTimeField(db_default=Now())
    linked_alert = models.ForeignKey(AlertInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "mortality_records"
        indexes = [
            models.Index(fields=["flock"], name="idx_mortality_flock"),
            models.Index(fields=["linked_alert"], name="idx_mortality_alert"),
        ]


class MedicalPrescription(models.Model):
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    flock = models.ForeignKey("telemetry.FlockCycle", on_delete=models.RESTRICT, related_name="+")
    doctor = models.ForeignKey("accounts.User", db_column="doctor_id", on_delete=models.RESTRICT, related_name="+")
    medication = models.CharField(max_length=200)
    dosage = models.CharField(max_length=100, null=True, blank=True)
    frequency = models.CharField(max_length=100, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, db_default=Now())
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "medical_prescriptions"
        indexes = [
            models.Index(fields=["flock"], name="idx_prescription_flock"),
        ]
