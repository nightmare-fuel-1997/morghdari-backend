"""Monitoring tasks (v0.7 M7-04/M7-08) — autodiscovered by config.celery.

فارسی: کارهای نظارتی؛ ضربانِ قلبِ بیت برای هشدار BeatHeartbeatStale و
کارِ شبانهٔ اندازه‌گیریِ مصرف هر سازمان برای MET-SAAS.

The BeatHeartbeatStale alert rule (monitoring/alerts.yml) reads
`celery_beat_heartbeat_timestamp_seconds` — a gauge exported by DAPHNE's
/metrics/ endpoint. Beat and daphne are separate processes, so this task
writes the unix timestamp to Redis and the scrape-time refresh
(monitoring.metrics.refresh_gauges) lifts it into the gauge. A dead beat
means the timestamp goes stale and the rule fires after 300 s.

M7-08 (MET-SAAS): `record_org_message` is the ingestion-side meter bump
(Saas.md §15 key `org:{org_id}:meter:current_hour`, TTL 3600) and
`build_org_usage` is the doc's nightly `aggregates.build_org_usage` rollup
(docs/data-aggregates-v1.md: stream counters + pg_total_relation_size
samples → org_daily_usage (upsert), 01:10 Asia/Tehran) that also exports
`tenant_cost_estimate_usd{org_id}` (Runbook §5.4: "Exported as
tenant_cost_estimate_usd{org_id} by a daily Celery job from v0.7") through
the Redis bridge so daphne's /metrics/ scrape picks it up.

Cross-tenant reads: the nightly rollup crosses orgs BY DESIGN, but the
runtime connection is morghdari_app — FORCE-RLS fail-closed on purpose. The
sanctioned bypass is the ADMIN role (Security §4.5; same precedent as
scripts/prod_bootstrap.sh platform paths), wired through the optional
`admin` DATABASES alias: base.py builds it from DATABASE_ADMIN_URL when the
env carries one (prod compose sets it on the worker), CI/laptop leave it
unset and the task falls back to `default` — whose connect user there is
the DB superuser and therefore already bypasses RLS. sqlite (laptop quick
loop) has neither RLS nor aliases; everything runs on `default`.
"""

import datetime as dt
import json
import time

from celery import shared_task
from django.db import connections
from django.db.models import Sum
from django.db.models.functions import Length
from django.utils import timezone

from apps.monitoring.metrics import bump_meter_hour, note_beat_heartbeat


@shared_task(name="monitoring.beat_heartbeat")
def beat_heartbeat() -> float:
    """Record the beat heartbeat timestamp (Redis bridge → daphne gauge)."""
    note_beat_heartbeat()
    return time.time()


@shared_task(name="monitoring.record_org_message")
def record_org_message(org_id, count: int = 1) -> int:
    """Ingestion-side meter bump: INCR org:{org_id}:meter:current_hour.

    Saas.md §15. The tier-allowance ENFORCEMENT consumer is P2 billing —
    v0.7 ships the honest meter the enforcement will read (MET-SAAS).
    """
    from apps.monitoring.metrics import _metrics_redis

    bump_meter_hour(_metrics_redis(), org_id, count)
    return count


# Fleet fixed-cost estimate (single-node Phase-1 rehearsal: the whole fleet
# is one host, so the fleet cost is the host cost). VPS-day: re-point to the
# real monthly invoice ÷ 30 — the FORMULA is what MET-SAAS fixes, the input
# is a deployment constant.
_FLEET_FIXED_COST_USD = 40.0


def _admin_alias() -> str:
    """The RLS-bypassing connection for cross-org nightly sweeps.

    `admin` when base.py could build it from DATABASE_ADMIN_URL (prod);
    otherwise `default` — in CI/laptop the connect user is the DB superuser
    and already bypasses RLS. sqlite: no aliases, no RLS → `default`.
    """
    return "admin" if "admin" in connections.databases else "default"


@shared_task(name="monitoring.build_org_usage")
def build_org_usage(day=None) -> int:
    """Nightly per-org rollup → org_daily_usage (upsert) + cost gauge.

    Sources (data-aggregates-v1.md `aggregates.build_org_usage`):
      * msg_count  — row count in the org's day window from saloon_telemetry
        (the DB is the durable daily ledger; the §15 meter is the live
        hourly guard),
      * stored_bytes — pg_total_relation_size('saloon_telemetry') × org
        message share (C-11: no columnstore; the doc's own attribution).
        sqlite has no relation-size machinery: the org's own serialized
        metrics bytes (len(json.dumps(metrics))) are used — same units,
        honest approximation,
      * saloon_count / gateway_count — DISTINCT saloon_id / slot_id in the
        org's day rows (0 on an empty day — an honest zero, not absence).

    Then `tenant_cost_estimate_usd{org_id}` = fixed cost × message share
    (§5.4: "fleet-level container cost divided by message share"), written
    through the Redis bridge; daphne exports it at scrape time. Zero-traffic
    orgs export an explicit 0.0 — absent ≠ free.
    """
    from apps.monitoring.metrics import K_TENANT_COST, _metrics_redis
    from apps.monitoring.models import OrgDailyUsage
    from apps.telemetry.models import SaloonTelemetry
    from apps.tenants.models import Organization

    target_day = day or (timezone.localdate() - dt.timedelta(days=1))
    client = _metrics_redis()
    alias = _admin_alias()

    # Tehran-day bucket (+03:30 fixed — Iran abolished DST 2022); the doc's
    # rollup cadence is "nightly 01:10 Asia/Tehran", so the day window must
    # be a Tehran day, not UTC.
    tehran = dt.timezone(dt.timedelta(hours=3, minutes=30))
    day_start = dt.datetime.combine(target_day, dt.time.min, tzinfo=tehran)
    day_end = day_start + dt.timedelta(days=1)

    orgs = list(
        Organization.objects.using(alias)
        .filter(is_active=True)
        .values_list("org_id", flat=True)
    )

    totals, saloons, gateways = {}, {}, {}
    for org in orgs:
        qs = SaloonTelemetry.objects.using(alias).filter(
            org_id=org, server_received_ts__gte=day_start, server_received_ts__lt=day_end
        )
        totals[org] = qs.count()
        saloons[org] = qs.values("saloon_id").distinct().count()
        gateways[org] = qs.values("slot_id").distinct().count()

    is_pg = connections[alias].vendor == "postgresql"
    total_bytes = 0
    if is_pg:
        with connections[alias].cursor() as cursor:
            cursor.execute("SELECT pg_total_relation_size('saloon_telemetry')")
            total_bytes = int(cursor.fetchone()[0] or 0)

    grand_total = sum(totals.values())

    upserts = 0
    for org in orgs:
        share = (totals[org] / grand_total) if grand_total else 0.0
        if is_pg:
            stored = int(total_bytes * share)
        else:
            # sqlite: no relation-size machinery — attribute the org's own
            # serialized metrics bytes instead (honest, same units).
            stored = (
                SaloonTelemetry.objects.using(alias)
                .filter(org_id=org, server_received_ts__gte=day_start, server_received_ts__lt=day_end)
                .aggregate(b=Sum(Length("metrics")))
                ["b"] or 0
            )
        defaults = {
            "msg_count": totals[org],
            "stored_bytes": stored,
            "saloon_count": saloons[org],
            "gateway_count": gateways[org],
        }
        OrgDailyUsage.objects.using(alias).update_or_create(
            org_id=org, day=target_day, defaults=defaults
        )
        upserts += 1

        cost = _FLEET_FIXED_COST_USD * share
        try:
            client.set(K_TENANT_COST.format(org=org), json.dumps(cost))
        except Exception:  # noqa: BLE001 — bridge failures must not fail the rollup
            pass

    return upserts
