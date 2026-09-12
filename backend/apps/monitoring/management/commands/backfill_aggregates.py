"""Backfill batch-1 daily aggregates over a Tehran-date range (v0.9 M9-10).

Ops-run: `manage.py backfill_aggregates --from YYYY-MM-DD [--to YYYY-MM-DD]`
(days ascending, every builder per day). Writers are idempotent upserts, so
re-runs and late-buffered replays correct rows instead of duplicating them —
run this after deploys, restores, or replay storms to close backfill gaps
(docs/data-aggregates-v1.md §4).
"""

import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from apps.monitoring.services_aggregates import (
    build_alert_stats,
    build_flock_snapshots,
    build_gateway_uptime,
    build_metric_daily,
)


class Command(BaseCommand):
    help = "Backfill batch-1 daily aggregates over a Tehran-date range (inclusive)."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="from_date", required=True, help="First Tehran day, YYYY-MM-DD.")
        parser.add_argument("--to", dest="to_date", default=None, help="Last Tehran day, YYYY-MM-DD (default: yesterday).")

    def handle(self, **options):
        try:
            first = dt.date.fromisoformat(options["from_date"])
            last = (
                dt.date.fromisoformat(options["to_date"])
                if options["to_date"]
                else dt.date.today() - dt.timedelta(days=1)
            )
        except ValueError as exc:
            raise CommandError(f"bad date: {exc}") from exc
        if last < first:
            raise CommandError("--to precedes --from")
        day = first
        while day <= last:
            metric = build_metric_daily(day)
            flocks = build_flock_snapshots(day)
            alerts = build_alert_stats(day)
            uptime = build_gateway_uptime(day)
            self.stdout.write(
                f"{day}: metric={metric} flocks={flocks} alerts={alerts} uptime={uptime}"
            )
            day += dt.timedelta(days=1)
