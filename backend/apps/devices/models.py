"""
Devices — edge gateways, sensor/actuator topology, registries, photos
(database.md Compartment 2).

فارسی: توپولوژی لبه — گیت‌وی، اسلات سنسور/اکچوئیتور، رجیستری سخت‌افزار و عکس‌های
نصب. نام جدول‌ها عیناً از داکیومنت است چون RLS و دموی psql با همین نام‌ها کار می‌کنند.

Identity pattern (doc): logical slot is PERMANENT; physical hardware rows come
and go through *_hardware_registry with installed_at/removed_at history, so a
relay swap never breaks command history or zone assignment.
"""

from django.db import models
from django.db.models import Q
from django.db.models.functions import Now


class GatewayRequest(models.Model):
    """Zero-touch provisioning request. NOTE (2026-08-23, Tracker §4i): the old
    subquery CHECK was removed from the doc — PostgreSQL CHECKs cannot reliably
    reference other rows; the max-pending limit is enforced at the application
    layer against system_configuration.max_pending_gateway_requests."""

    id = models.AutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    farm = models.ForeignKey("farms.Farm", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    requested_by = models.ForeignKey("accounts.User", db_column="requested_by", on_delete=models.RESTRICT, related_name="+")
    requested_at = models.DateTimeField(null=True, db_default=Now())
    approved_by = models.ForeignKey("accounts.User", db_column="approved_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="PENDING")
    rejection_reason = models.TextField(null=True, blank=True)
    details = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "gateway_requests"
        constraints = [
            models.CheckConstraint(
                check=Q(status__in=["PENDING", "APPROVED", "REJECTED"]),
                name="ck_gateway_requests_status",
            ),
        ]


class GatewaySlot(models.Model):
    slot_id = models.CharField(max_length=50, primary_key=True)
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    farm = models.ForeignKey("farms.Farm", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    position_label = models.CharField(max_length=100, null=True, blank=True)
    request = models.ForeignKey(GatewayRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # R4 zero-touch initial state; PENDING_SYNC = legacy installer-PWA path only.
    provisioning_status = models.CharField(max_length=30, default="PENDING_INSTALLATION")
    created_at = models.DateTimeField(null=True, db_default=Now())
    # Who registered this slot (Hardware Team).
    created_by = models.ForeignKey("accounts.User", db_column="created_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "gateway_slots"
        constraints = [
            models.CheckConstraint(
                check=Q(provisioning_status__in=[
                    "PENDING_SYNC",
                    "PENDING_INSTALLATION",
                    "PENDING_APPROVAL",
                    "PROVISIONED",
                    "REJECTED",
                    "SUSPENDED",
                ]),
                name="ck_gateway_slots_provisioning_status",
            ),
        ]


class GatewayHardwareRegistry(models.Model):
    id = models.AutoField(primary_key=True)
    hardware_id = models.CharField(max_length=100)
    slot = models.ForeignKey(GatewaySlot, on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    # AU-RLS 2026-08-16.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    installed_at = models.DateTimeField(db_default=Now())
    removed_at = models.DateTimeField(null=True, blank=True)
    installed_by = models.ForeignKey("accounts.User", db_column="installed_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    removed_by = models.ForeignKey("accounts.User", db_column="removed_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "gateway_hardware_registry"
        indexes = [
            models.Index(
                fields=["slot"],
                name="idx_active_gateway_hw",
                condition=Q(removed_at__isnull=True),
            ),
        ]


class GatewaySaloonZone(models.Model):
    """Gateway ↔ saloon zone assignment.

    CONSTRAINTS C-07: the doc declares PRIMARY KEY (gateway_slot_id, saloon_id).
    Composite PKs need Django ≥5.2; the §2 pin is <5.1, so a surrogate id +
    unique constraint carries the same uniqueness semantics until the owner
    decides otherwise (retrospective input).
    """

    id = models.AutoField(primary_key=True)
    gateway_slot = models.ForeignKey(GatewaySlot, on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    # AU-RLS 2026-08-16.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    assigned_at = models.DateTimeField(null=True, db_default=Now())
    assigned_by = models.ForeignKey("accounts.User", db_column="assigned_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "gateway_saloon_zones"
        constraints = [
            models.UniqueConstraint(fields=["gateway_slot", "saloon"], name="uq_gateway_saloon_zones_pk"),
        ]


class SaloonPosition(models.Model):
    """Named physical positions ('North Wall'...): hierarchy Saloon → Position →
    Sensor Slot; multiple sensor packs may share one position."""

    id = models.AutoField(primary_key=True)
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, related_name="+")
    # AU-RLS 2026-08-16.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    label = models.CharField(max_length=100)
    created_by = models.ForeignKey("accounts.User", db_column="created_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "saloon_positions"
        constraints = [
            models.UniqueConstraint(fields=["saloon", "label"], name="uq_saloon_positions_saloon_label"),
        ]


class SaloonSensorSlot(models.Model):
    slot_id = models.CharField(max_length=50, primary_key=True)
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, related_name="+")
    # AU-RLS 2026-08-16.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    position = models.ForeignKey(SaloonPosition, on_delete=models.RESTRICT, related_name="+")
    # AU-fix 2026-08-16: column referenced by LOS watchdog + OpenAPI but was
    # missing from DDL — do not remove again.
    is_active = models.BooleanField(default=True)
    allowed_metric_keys = models.JSONField(default=list)

    class Meta:
        db_table = "saloon_sensor_slots"


class HardwareRegistry(models.Model):
    """Sensor hardware install history (same pattern as gateway registry)."""

    id = models.AutoField(primary_key=True)
    hardware_id = models.CharField(max_length=100)
    slot = models.ForeignKey(SaloonSensorSlot, on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    # AU-RLS 2026-08-16.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    installed_at = models.DateTimeField(db_default=Now())
    removed_at = models.DateTimeField(null=True, blank=True)
    installed_by = models.ForeignKey("accounts.User", db_column="installed_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    removed_by = models.ForeignKey("accounts.User", db_column="removed_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "hardware_registry"
        indexes = [
            models.Index(
                fields=["slot"],
                name="idx_active_sensor_hw",
                condition=Q(removed_at__isnull=True),
            ),
        ]


class GatewaySensorMapping(models.Model):
    """Maps gateway hardware + local pack id → logical sensor slot.

    AU-fix 2026-08-16: this table previously had NO writer — every gateway's
    telemetry would dead-letter as UNKNOWN_SLOT on day one. Canonical writer:
    POST /api/v1/provisioning/sync/ (Saas.md §9), v0.5.

    CONSTRAINTS C-08: the doc's FK targets gateway_hardware_registry(hardware_id),
    which has NO unique constraint (install history implies repeats). PostgreSQL
    rejects such an FK outright; enforcing UNIQUE would break reinstall history.
    Shipped as db_constraint=False pending the owner decision — see C-08.
    """

    id = models.AutoField(primary_key=True)
    # CONSTRAINTS C-08: the doc writes this as REFERENCES
    # gateway_hardware_registry(hardware_id), but hardware_id has NO unique
    # constraint (install history implies repeats) and PostgreSQL refuses such
    # an FK outright — the doc's clause is un-executable as written. Shipped as
    # a plain column pending the owner decision (enforce UNIQUE vs drop the FK).
    gateway_hardware_id = models.CharField(max_length=100)
    # Gateway-local pack identifier (e.g., "P-A").
    pack_id = models.CharField(max_length=50)
    slot = models.ForeignKey(SaloonSensorSlot, on_delete=models.RESTRICT, related_name="+")
    # AU-RLS 2026-08-16.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    created_at = models.DateTimeField(null=True, db_default=Now())
    created_by = models.ForeignKey("accounts.User", db_column="created_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "gateway_sensor_mapping"
        constraints = [
            models.UniqueConstraint(fields=["gateway_hardware_id", "pack_id"], name="uq_gw_sensor_mapping_hw_pack"),
        ]
        indexes = [
            # Redundant-with-unique but verbatim from the doc.
            models.Index(fields=["gateway_hardware_id", "pack_id"], name="idx_gw_sensor_mapping"),
        ]


class SaloonPhoto(models.Model):
    """Photos taken by installers during provisioning."""

    id = models.AutoField(primary_key=True)
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, related_name="+")
    # AU-RLS 2026-08-16.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    uploaded_by = models.ForeignKey("accounts.User", db_column="uploaded_by", on_delete=models.RESTRICT, related_name="+")
    uploaded_at = models.DateTimeField(null=True, db_default=Now())
    file_url = models.CharField(max_length=500)
    description = models.CharField(max_length=255, null=True, blank=True)
    is_primary = models.BooleanField(default=False, null=True)

    class Meta:
        db_table = "saloon_photos"
        constraints = [
            # Verbatim partial unique index idx_saloon_photos_primary.
            models.UniqueConstraint(
                fields=["saloon"],
                condition=Q(is_primary=True),
                name="idx_saloon_photos_primary",
            ),
        ]


class SaloonActuatorSlot(models.Model):
    """Actuator hierarchy: Saloon → Position → Actuator Slot (logical) →
    Actuator Hardware (physical). An actuator is anything commandable: heater,
    exhaust_fan, curtain. The slot is permanent — see module docstring."""

    slot_id = models.CharField(max_length=50, primary_key=True)  # e.g. 'S01-HEATER-A'
    # Denormalized for RLS enforcement.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, related_name="+")
    position = models.ForeignKey(SaloonPosition, on_delete=models.RESTRICT, related_name="+")
    actuator_type = models.CharField(max_length=30)
    created_at = models.DateTimeField(null=True, db_default=Now())
    created_by = models.ForeignKey("accounts.User", db_column="created_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "saloon_actuator_slots"
        constraints = [
            models.CheckConstraint(
                check=Q(actuator_type__in=["heater", "exhaust_fan", "curtain"]),
                name="ck_saloon_actuator_slots_actuator_type",
            ),
        ]


class ActuatorHardwareRegistry(models.Model):
    id = models.AutoField(primary_key=True)
    hardware_id = models.CharField(max_length=100)
    slot = models.ForeignKey(SaloonActuatorSlot, on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    installed_at = models.DateTimeField(db_default=Now())
    removed_at = models.DateTimeField(null=True, blank=True)
    installed_by = models.ForeignKey("accounts.User", db_column="installed_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    removed_by = models.ForeignKey("accounts.User", db_column="removed_by", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "actuator_hardware_registry"
        indexes = [
            models.Index(
                fields=["slot"],
                name="idx_active_actuator_hw",
                condition=Q(removed_at__isnull=True),
            ),
        ]
