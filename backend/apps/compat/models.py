"""LegacySlugMap — the ONLY schema addition of the compat layer (M6-09).

Contract v1.6.0-draft §2 verbatim shape (docs/api-compat-flutter-v1.md):
Flutter `slug` == opaque alias resolved through this table, never a renamed
PK. Flutter `device` == backend `saloon` (§1.3 of the spec), so `saloon_id`
is set for kind='device'. Tenancy: org_id is denormalized so every resolver
can filter by the JWT claim AFTER resolving the slug — a guessed foreign slug
yields 404, never cross-org data (Security §4.1 branch ③).

managed=False ON PURPOSE: the compat layer is additive to a database whose
core is frozen (database.md). The table is created by this app's RunSQL
migration (§2 DDL verbatim, incl. the CHECK and both indexes); declaring the
model managed would let Django believe it owns the table and eventually emit
ALTERs — the exact thing the hard rule forbids.

فارسی: نگاشتِ شناسهٔ قدیمی — تنها جدولِ تازهٔ لایهٔ سازگاری؛ اَن‌منیج تا جنگو
هرگز ALTERی روی دیتابیسِ منجمد صادر نکند.
"""

from django.db import models
from django.db.models.functions import Now


class LegacySlugMap(models.Model):
    slug = models.CharField(max_length=64, primary_key=True)
    kind = models.CharField(max_length=10)  # CHECK ('org','farm','device') in the DDL
    org_id = models.UUIDField()
    farm_id = models.IntegerField(null=True)
    saloon_id = models.IntegerField(null=True)
    # db_default keeps INSERTs emitting the column DEFAULT (the DDL's NOW())
    # instead of an explicit NULL — the column is NOT NULL in the DDL.
    created_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "legacy_slug_map"
        managed = False
