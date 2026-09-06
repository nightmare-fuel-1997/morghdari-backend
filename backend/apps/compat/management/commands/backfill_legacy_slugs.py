"""Compat management command — the idempotent legacy_slug_map backfill.

Ops-run (spec §2 write path: backfill + admin-only creation; compat READS
never write). One row per existing organizations/farms/saloons row with the
suggested stable values (org-<first-8-of-uuid>, farm-<id>, device-<saloon_id>);
re-runs change nothing (get_or_create on the slug PK). Prints the resulting
slug table for the Flutter team.

فارسی: دستورِ پرکردنِ اولیهٔ نگاشتِ شناسه‌ها — تکرارپذیر و فقط از مسیرِ عملیاتی.
"""

from django.core.management.base import BaseCommand

from apps.compat.models import LegacySlugMap
from apps.farms.models import Farm, Saloon
from apps.tenants.models import Organization


class Command(BaseCommand):
    help = "Idempotently seed legacy_slug_map from existing topology rows."

    def handle(self, **options):
        created = 0
        for org in Organization.objects.all().order_by("org_id"):
            slug = f"org-{str(org.org_id).split('-')[0]}"
            _, was_created = LegacySlugMap.objects.get_or_create(
                slug=slug,
                defaults={
                    "kind": "org", "org_id": org.org_id,
                    "farm_id": None, "saloon_id": None,
                },
            )
            created += int(was_created)

        for farm in Farm.objects.filter(org__isnull=False).order_by("pk"):
            slug = f"farm-{farm.pk}"
            _, was_created = LegacySlugMap.objects.get_or_create(
                slug=slug,
                defaults={
                    "kind": "farm", "org_id": farm.org_id,
                    "farm_id": farm.pk, "saloon_id": None,
                },
            )
            created += int(was_created)

        for saloon in Saloon.objects.all().order_by("pk"):
            slug = f"device-{saloon.pk}"
            _, was_created = LegacySlugMap.objects.get_or_create(
                slug=slug,
                defaults={
                    "kind": "device", "org_id": saloon.org_id,
                    "farm_id": saloon.farm_id, "saloon_id": saloon.pk,
                },
            )
            created += int(was_created)

        self.stdout.write(f"backfill complete — {created} row(s) created")
        for row in LegacySlugMap.objects.all().order_by("kind", "slug"):
            self.stdout.write(f"  {row.kind:6} {row.slug:24} org={row.org_id} farm={row.farm_id} saloon={row.saloon_id}")
