"""
Telemetry — TimescaleDB time-series core (database.md Compartment 3).

فارسی: هستهٔ سری-زمانی. دو جدول hypertable «مدیریت‌نشده»اند چون Django نمی‌تواند
hypertable/فشرده‌سازی/retention بسازد — DDL کامل آن‌ها در مهاجرت RunSQL همین اپ است.

Unmanaged models (saloon_telemetry, metric_hourly_aggregates):
- managed=False because create_hypertable / compression / retention policies are
  pure TimescaleDB DDL (Blueprint/TUTORIAL §3.9). The FULL table DDL therefore
  lives in this app's RunSQL migration — including UNIQUE constraints and
  indexes, which are deliberately NOT declared in Meta here (Django would emit
  AddConstraint ops that duplicate the RunSQL DDL).
"""

from django.db import models
from django.db.models.functions import Now


class SensorMetricType(models.Model):
    """Metric catalog — new sensors require ZERO schema migrations; add a row,
    gateway sends the key, ingestion auto-discovers (doc Compartment 3 header).
    Org-less global: NO org_id, NO RLS policy (TUTORIAL §3.9)."""

    metric_key = models.CharField(max_length=20, primary_key=True)
    display_name = models.CharField(max_length=100)
    unit = models.CharField(max_length=20)  # 'C', '%', 'ppm', 'lux'
    value_type = models.CharField(max_length=20, default="numeric", null=True)
    # Enables velocity emergency checks.
    is_tier1 = models.BooleanField(default=False, null=True)
    icon_name = models.CharField(max_length=50, null=True)  # frontend icon identifier

    class Meta:
        db_table = "sensor_metric_types"


class SaloonTelemetry(models.Model):
    """Raw telemetry hypertable (UNMANAGED — full DDL in RunSQL migration).

    Naming note (doc): DB persists slot_id even though the payload says pack_id.
    Dedup warning (Audit Gap-Idem): the doc's UNIQUE is for PARTITION INTEGRITY
    only — real dedup happens upstream via the Redis idempotency guard on
    (org_id, slot_id, device_ts), Saas.md Decision 14 (v0.3 concern).
    """

    # PK pragmatism: the REAL uniqueness is composite (server_received_ts,
    # slot_id, org_id) via RunSQL — Django can't express that, and an implied
    # auto `id` would SELECT a column this hypertable does not have. We pin
    # the deterministic component instead; never rely on it as an identity
    # guarantee (that's the Redis dedup guard's job, Saas Decision 14).
    # فارسی: کلید اصلیِ جنگو صرفاً برای سازگاریِ SELECT است؛ تضمین یکتایی نزد
    # گاردِ ردیس است نه اینجا.
    server_received_ts = models.DateTimeField(db_default=Now(), primary_key=True)
    device_ts = models.DateTimeField()
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", db_column="saloon_id", on_delete=models.RESTRICT, related_name="+")
    slot = models.ForeignKey("devices.SaloonSensorSlot", db_column="slot_id", on_delete=models.RESTRICT, related_name="+")
    is_buffered = models.BooleanField(default=False, null=True)
    # NOTE: `buffered_delay_mins` is a DB-GENERATED column (NUMERIC(10,2)
    # STORED from the ts delta — full DDL in the RunSQL migration). It is
    # deliberately ABSENT here because Django <5.2 cannot express
    # GENERATED ALWAYS and would emit a NULL insert (rejected by Postgres).
    # Read it via SaloonTelemetry.objects.annotate(...) where needed.
    # فارسی: ستونِ تولیدشده در مدل نمی‌آید چون جنگو هنوز آن را پشتیبانی نمی‌کند؛
    # هنگام خواندن با annotate اضافه می‌شود.
    metrics = models.JSONField(default=dict)
    clock_drift = models.BooleanField(default=False, null=True)

    class Meta:
        db_table = "saloon_telemetry"
        managed = False


class MetricHourlyAggregate(models.Model):
    """Celery-built permanent hourly summaries (UNMANAGED — full DDL in RunSQL).

    NOT subject to the 180-day retention of raw telemetry: year-over-year
    summaries are permanent by design (AU-fix 2026-08-16).
    high_variance is DB-generated (Tracker §4i P2): coefficient-of-variation
    rule stddev > 15% of |avg| replaces the meaningless stddev > 2*avg.
    """

    # Declarative pk only — the real table has plain BIGSERIAL with NO PK
    # (Timescale requires partition column inside any unique/PK index); the
    # uniqueness lives in the RunSQL UNIQUE(bucket_hr, slot_id, metric_key).
    id = models.BigAutoField(primary_key=True)
    bucket_hr = models.DateTimeField()  # time_bucket('1 hour', server_received_ts)
    org_id = models.UUIDField()
    saloon_id = models.IntegerField()
    slot_id = models.CharField(max_length=50)
    metric = models.ForeignKey(SensorMetricType, db_column="metric_key", on_delete=models.RESTRICT, related_name="+")
    avg_val = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    min_val = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    max_val = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    stddev_val = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    sample_count = models.IntegerField(default=0)
    high_variance = models.BooleanField(editable=False, null=True)  # DB-generated

    class Meta:
        db_table = "metric_hourly_aggregates"
        managed = False


class FlockCycle(models.Model):
    """Production cycle per saloon. MET-FCR/MET-KPI 2026-08-22: placement_count
    + day_old_weight_g exist so mortality % and FCR are computable.

    Transition logic (doc ⚠️): setting end_date is an explicit 'flock complete'
    signal — post_save re-caches thresholds immediately; the midnight cron is
    the backstop. Signals arrive with the alerting version (v0.4)."""

    flock_id = models.AutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", db_column="org_id", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", db_column="saloon_id", on_delete=models.RESTRICT, related_name="+")
    breed_type = models.CharField(max_length=50)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField(null=True, blank=True)
    placement_count = models.IntegerField()  # chicks placed at flock start (mortality-% denominator)
    day_old_weight_g = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "flock_cycles"
        indexes = [
            models.Index(fields=["org", "saloon", "start_date", "end_date"], name="idx_flock_lookup"),
        ]
