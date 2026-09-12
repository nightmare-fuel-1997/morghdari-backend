"""
Farms & saloons — the physical top of the topology (database.md Compartment 2).

فارسی: مزرعه و سالن؛ بالای درخت توپولوژی فیزیکی. حذفِ محدود عمدی است تا
حذف اتفاقی یک سالن، تاریخچهٔ تلمتری را پاک نکند (ON DELETE RESTRICT).
"""

from django.db import models
from django.db.models.functions import Now


class Farm(models.Model):
    id = models.AutoField(primary_key=True)
    # Nullable per doc — farm row may precede org assignment completing.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, null=True, related_name="+")
    name = models.CharField(max_length=255)
    province = models.ForeignKey("geography.Province", on_delete=models.RESTRICT, related_name="+")
    city = models.ForeignKey("geography.City", on_delete=models.RESTRICT, related_name="+")
    village = models.ForeignKey("geography.Village", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "farms"


class Saloon(models.Model):
    id = models.AutoField(primary_key=True)
    farm = models.ForeignKey("farms.Farm", on_delete=models.RESTRICT, null=True, related_name="+")
    # v1.7.1 (2026-09-12): EXPLICIT saloon → template assignment — the manager's
    # "this house, this template" lever, and the FIRST link of the resolution
    # chain (assignment → org+breed published → org default → global default;
    # apps.alerts.services.thresholds.effective_template). NULL = inherit the
    # breed/default chain, which is exactly how every saloon behaved before this
    # column existed. RESTRICT + no delete surface: a template is deprecated,
    # never deleted out from under the saloons (and history) pointing at it.
    threshold_template = models.ForeignKey(
        "alerts.LifecycleTemplate",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="assigned_saloons",
    )
    # AU-RLS 2026-08-16: denormalized for RLS.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "saloons"
