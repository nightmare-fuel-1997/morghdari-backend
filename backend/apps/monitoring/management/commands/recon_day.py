"""Daily reconciliation ledger (v0.9 M9-10/B5, M9-02 tooling).

Ops-run: `manage.py recon_day [--day YYYY-MM-DD] [--org UUID]` — the same
report as GET /reconciliation/, printable from cron into the pilot log.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.monitoring.ops_services import recon_day_report


class Command(BaseCommand):
    help = "Print one Tehran day's delivery ledger (DB rows + DLQ rows + alerts)."

    def add_arguments(self, parser):
        parser.add_argument("--day", default=None, help="Tehran day, YYYY-MM-DD (default: yesterday).")
        parser.add_argument("--org", default=None, help="Org UUID (default: all orgs).")

    def handle(self, **options):
        try:
            report = recon_day_report(options["day"], org_id=options["org"])
        except ValueError as exc:
            raise CommandError(f"bad --day: {exc}") from exc
        for org in report["orgs"]:
            self.stdout.write(
                f"{org['org_id']} db={org['db_rows']} dlq={org['dlq_rows']} "
                f"accounted={org['accounted_pct']}% alerts={len(org['alerts'])} "
                f"reasons={json.dumps(org['by_reason'], sort_keys=True)}"
            )
