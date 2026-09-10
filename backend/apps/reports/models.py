"""reports models — database.md verbatim (v0.7 M7-15, Tracker §4n/§4s).

Four tables, byte-for-byte the doc's shapes:

- daily_reports: one row per (flock, saloon, report_date). The state machine
  is the doc's — DRAFT → SUBMITTED → APPROVED/REJECTED, plus the FLAGGED
  branch: any medication line forces FLAGGED atomically with the auto
  medical ticket + WS push to the doctor (D24-8).
- report_medication_usages: medication lines on a report. medication_name is
  denormalized so the row survives catalog_item_id ON DELETE SET NULL (the
  catalog item can be deactivated/deleted; the usage record must not).
- vet_medication_catalog: the per-org medication dictionary — the FK target
  for medication lines. Table-only in M7-15 (its /vet-catalog/ endpoint is
  v0.9 scope); created here because report_medication_usages REFERENCES it.
- support_tickets: the general ticket table (extends the doctor_tickets
  pattern — §4n explicitly forbids a new messaging system). category=medical
  rows are auto-created by the FLAGGED branch with linked_report_id set.

All four carry org_id with the doc's RLS comment: tenant_isolation
(Tracker §4n). The FORCE-RLS policies land in migrations/0001 (vendor-guarded,
M7-08 org_daily_usage pattern); the models themselves are unmanaged so
pytest's --nomigrations sqlite loop never tries to CREATE TABLE.

فارسی: خانوادهٔ گزارش‌های روزانه — چهار جدول دقیقاً طبق database.md؛ خطِ
داروگزارش، وضعیتِ FLAGGED را با تیکتِ پزشکیِ خودکار و ارسالِ وب‌سوکت به
دکتر هم‌زمان می‌کند (D24-8).
"""

from django.db import models
from django.db.models import F
from django.db.models.functions import Now

from apps.accounts.models import User


class DailyReport(models.Model):
    id = models.AutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    farm = models.ForeignKey("farms.Farm", db_column="farm_id", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", db_column="saloon_id", on_delete=models.RESTRICT, related_name="+")
    flock = models.ForeignKey("telemetry.FlockCycle", db_column="flock_id", on_delete=models.RESTRICT, related_name="+")
    report_date = models.DateField()  # calendar day in Asia/Tehran
    mortality_count = models.IntegerField(default=0)
    feed_kg = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    water_l = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg_weight_g = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, default="DRAFT")
    submitted_by = models.ForeignKey(User, db_column="submitted_by", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, db_column="reviewed_by", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(null=True, blank=True)
    vet_approved_by = models.ForeignKey(User, db_column="vet_approved_by", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    vet_approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "daily_reports"
        managed = False
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=["DRAFT", "SUBMITTED", "FLAGGED", "APPROVED", "REJECTED"]),
                name="ck_daily_reports_status",
            ),
            models.UniqueConstraint(fields=["flock", "saloon", "report_date"], name="uq_daily_reports_flock_saloon_date"),
        ]
        indexes = [
            models.Index(F("org"), F("report_date").desc(), name="idx_reports_org"),
            models.Index(F("saloon"), F("report_date").desc(), name="idx_reports_saloon"),
        ]


class ReportMedicationUsage(models.Model):
    id = models.AutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    report = models.ForeignKey(DailyReport, db_column="report_id", on_delete=models.CASCADE, related_name="medications")
    catalog_item = models.ForeignKey("reports.VetMedicationCatalog", db_column="catalog_item_id", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    medication_name = models.CharField(max_length=200)
    dosage = models.CharField(max_length=100, null=True, blank=True)
    frequency = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        db_table = "report_medication_usages"
        managed = False
        indexes = [
            models.Index(fields=["org", "report"], name="idx_medusage_org"),
        ]


class DailyReportExtras(models.Model):
    """v0.7 M7-16 (§4s R-02 merge) — the 1:1 side table for the fields the
    Flutter form carries that v1.5.0 daily_reports never had. Zero ALTER on
    the frozen table (BN-02 saloon_profile pattern); rows are created
    LAZILY on the first write carrying a new field — canonical-only reports
    get no row at all (zero overhead)."""

    report = models.OneToOneField(DailyReport, db_column="report_id", on_delete=models.CASCADE, related_name="extras", primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    bird_count = models.IntegerField(null=True, blank=True)
    avg_temperature_c = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    avg_humidity_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    ventilation_status = models.CharField(max_length=20, null=True, blank=True)
    health_status = models.CharField(max_length=20, null=True, blank=True)
    checklist_feed = models.BooleanField(default=False)
    checklist_water = models.BooleanField(default=False)
    checklist_device = models.BooleanField(default=False)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "daily_report_extras"
        managed = False
        constraints = [
            models.CheckConstraint(
                check=models.Q(ventilation_status__in=["STABLE", "NEEDS_REVIEW", "PROBLEM"]),
                name="ck_daily_report_extras_ventilation",
            ),
            models.CheckConstraint(
                check=models.Q(health_status__in=["NORMAL", "WARNING", "CRITICAL"]),
                name="ck_daily_report_extras_health",
            ),
        ]
        indexes = [
            models.Index(fields=["org"], name="idx_report_extras_org"),
        ]


class VetMedicationCatalog(models.Model):
    id = models.AutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    created_by = models.ForeignKey(User, db_column="created_by", on_delete=models.RESTRICT, related_name="+")
    name = models.CharField(max_length=200)
    dosage_default = models.CharField(max_length=100, null=True, blank=True)
    unit = models.CharField(max_length=20, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "vet_medication_catalog"
        managed = False
        constraints = [
            models.UniqueConstraint(fields=["org", "name"], name="uq_vet_medication_catalog_org_name"),
        ]
        indexes = [
            models.Index(fields=["org", "is_active"], name="idx_vetcat_org"),
        ]


class SupportTicket(models.Model):
    id = models.AutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", db_column="saloon_id", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    raised_by = models.ForeignKey(User, db_column="raised_by", on_delete=models.RESTRICT, related_name="+")
    assigned_to = models.ForeignKey(User, db_column="assigned_to", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    category = models.CharField(max_length=20)
    linked_report = models.ForeignKey(DailyReport, db_column="linked_report_id", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, default="OPEN")
    priority = models.CharField(max_length=10, default="NORMAL")
    created_at = models.DateTimeField(null=True, db_default=Now())
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "support_tickets"
        managed = False
        constraints = [
            models.CheckConstraint(check=models.Q(category__in=["medical", "support", "installer", "report"]), name="ck_support_tickets_category"),
            models.CheckConstraint(check=models.Q(status__in=["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"]), name="ck_support_tickets_status"),
            models.CheckConstraint(check=models.Q(priority__in=["LOW", "NORMAL", "HIGH", "URGENT"]), name="ck_support_tickets_priority"),
        ]
        indexes = [
            models.Index(fields=["org", "status"], name="idx_suptickets_org"),
            models.Index(
                models.F("assigned_to"),
                condition=models.Q(status__in=["OPEN", "IN_PROGRESS"]),
                name="idx_suptickets_assignee",
            ),
        ]
