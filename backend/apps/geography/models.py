"""
Geography — shared hierarchical reference tree (database.md Compartment 1 tail).

فارسی: جدول‌های مرجع جغرافیایی (استان/شهر/روستا) — مشترک بین همهٔ سازمان‌ها،
بدون org_id و بدون سیاست RLS (جدول‌های global بی‌سازمان).

Organization → Province → City → Village → Farm; used by the Device Installer
panel for location-based provisioning workflows. Org-less globals deliberately
get NO tenant_isolation policy (TUTORIAL §3.9).
"""

from django.db import models


class Province(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        db_table = "provinces"


class City(models.Model):
    id = models.AutoField(primary_key=True)
    province = models.ForeignKey(Province, on_delete=models.RESTRICT, related_name="+")
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "cities"
        constraints = [
            models.UniqueConstraint(fields=["province", "name"], name="uq_cities_province_name"),
        ]


class Village(models.Model):
    id = models.AutoField(primary_key=True)
    city = models.ForeignKey(City, on_delete=models.RESTRICT, related_name="+")
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "villages"
        constraints = [
            models.UniqueConstraint(fields=["city", "name"], name="uq_villages_city_name"),
        ]
