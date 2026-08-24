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
    # AU-RLS 2026-08-16: denormalized for RLS.
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, related_name="+")
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "saloons"
