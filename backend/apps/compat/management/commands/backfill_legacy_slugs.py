"""Compat management command — the idempotent legacy_slug_map backfill.

Ops-run (spec §2 write path: backfill + admin-only creation; compat READS
never write). One row per existing organizations/farms/saloons row; re-runs
change nothing. Since v1.7.2 the FORMAT lives in `apps.compat.legacy_slugs` —
the same helpers the topology write path (POST /farms/, POST saloons/) calls in
transaction, so a farm created through the API and a farm backfilled here can
never carry different slugs. This command is now the REPAIR tool: it catches up
rows that predate the write path (or a stack whose compat DDL was imported).

فارسی: دستورِ پرکردنِ اولیهٔ نگاشتِ شناسه‌ها — تکرارپذیر؛ قالبِ شناسه‌ها اکنون
در یک ماژولِ مشترک با مسیرِ نوشتنِ توپولوژی است تا هرگز واگرا نشوند.
"""

from django.core.management.base import BaseCommand

from apps.compat.legacy_slugs import (
    LegacySlugMap,
    mint_farm_slug,
    mint_org_slug,
    mint_saloon_slug,
    slug_table_available,
)
from apps.farms.models import Farm, Saloon
from apps.tenants.models import Organization


class Command(BaseCommand):
    help = "Idempotently seed legacy_slug_map from existing topology rows."

    def handle(self, **options):
        if not slug_table_available():
            self.stdout.write(
                "legacy_slug_map is Postgres-only (compat/0001 DDL) — nothing to do here."
            )
            return
        before = LegacySlugMap.objects.count()
        for org in Organization.objects.all().order_by("org_id"):
            mint_org_slug(org)
        for farm in Farm.objects.filter(org__isnull=False).order_by("pk"):
            mint_farm_slug(farm)
        for saloon in Saloon.objects.all().order_by("pk"):
            mint_saloon_slug(saloon)

        created = LegacySlugMap.objects.count() - before
        self.stdout.write(f"backfill complete — {created} row(s) created")
        for row in LegacySlugMap.objects.all().order_by("kind", "slug"):
            self.stdout.write(f"  {row.kind:6} {row.slug:24} org={row.org_id} farm={row.farm_id} saloon={row.saloon_id}")
