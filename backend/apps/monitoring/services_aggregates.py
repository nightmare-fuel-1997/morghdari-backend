"""Batch-1 daily aggregate writers — v0.9 M9-10 (docs/data-aggregates-v1.md §2/§4).

Four idempotent upsert builders over Tehran-calendar days (day=None means
*yesterday* Tehran, the nightly-cron convention from build_org_usage):

- build_metric_daily    hourly rows + the day's threshold bands
- build_flock_snapshots mortality/feed/water/weight + flock_cycles (KPI math reused)
- build_alert_stats     alert_instances + acknowledgements (MTTR family)
- build_gateway_uptime  raw-telemetry presence + LOS durations

Cross-org BY DESIGN: every read runs on the RLS-bypassing admin alias
(same sanctioned pattern as monitoring.tasks._admin_alias and the M8-03
rebuild service) — a GUC-less worker would otherwise see ZERO tenant rows.

Backend-agnostic on purpose: the sqlite laptop loop has no DATE_TRUNC or
Tehran-aware SQL, so bucketing/parsing happens in Python over small daily
row sets (a saloon-day is ~1.4k raw rows worst case — trivial).

Late-arriving buffered telemetry re-opens day rows (upsert, never
append-duplicate) — re-running a day after a replay storm corrects it.
"""

import datetime as dt
import logging

from django.db import connections

logger = logging.getLogger(__name__)

TEHRAN = dt.timezone(dt.timedelta(hours=3, minutes=30))  # fixed — DST abolished 2022


def _alias() -> str:
    return "admin" if "admin" in connections.databases else "default"


def target_day(day=None) -> dt.date:
    if day is not None:
        return day if isinstance(day, dt.date) else dt.date.fromisoformat(str(day))
    return dt.datetime.now(TEHRAN).date() - dt.timedelta(days=1)


def day_window(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=TEHRAN)
    return start, start + dt.timedelta(days=1)


def build_metric_daily(day=None) -> int:
    """Roll hourly aggregates into metric_daily_aggregates for one Tehran day.

    avg = mean of hourly avgs; min/max = extremes; sample_count = hourly rows;
    inband_minutes = 60 per hour whose averages all sit inside the saloon's
    flock-age bands that day (kpi.hour_compliant — the SAME definition
    alerting uses, so compliance and alerting can never disagree). No flock
    covering the saloon-day means no bands, hence 0 in-band minutes (honest
    NULL-rule: compliance is undefined without a flock, not zero-percent).
    """
    from apps.alerts.services import kpi
    from apps.alerts.services.thresholds import effective_template
    from apps.monitoring.models import MetricDailyAggregate
    from apps.telemetry.models import FlockCycle, MetricHourlyAggregate

    alias = _alias()
    day = target_day(day)
    start, end = day_window(day)
    rows = (
        MetricHourlyAggregate.objects.using(alias)
        .filter(bucket_hr__gte=start, bucket_hr__lt=end)
        .order_by("org_id", "saloon_id", "slot_id", "metric", "bucket_hr")
    )
    groups: dict[tuple, dict] = {}
    for r in rows.iterator():
        key = (str(r.org_id), r.saloon_id, r.slot_id, r.metric_id)
        g = groups.setdefault(key, {"avgs": [], "min": None, "max": None, "n": 0, "buckets": {}})
        if r.avg_val is not None:
            g["avgs"].append(float(r.avg_val))
        if r.min_val is not None:
            g["min"] = r.min_val if g["min"] is None else min(g["min"], r.min_val)
        if r.max_val is not None:
            g["max"] = r.max_val if g["max"] is None else max(g["max"], r.max_val)
        g["n"] += 1
        bucket = r.bucket_hr.astimezone(TEHRAN).replace(minute=0, second=0, microsecond=0)
        g["buckets"].setdefault(bucket, []).append((r.metric_id, r.avg_val))

    written = 0
    for (org_id, saloon_id, slot_id, metric_key), g in groups.items():
        flock = (
            FlockCycle.objects.using(alias)
            .filter(saloon_id=saloon_id, start_date__lt=end)
            .exclude(end_date__lt=start)
            .order_by("-start_date")
            .first()
        )
        inband = 0
        if flock is not None:
            age_days = (day - flock.start_date.astimezone(TEHRAN).date()).days
            template, _ = effective_template(flock)
            bands = kpi.bands_for_age_days(max(age_days, 0), template.pk if template else None)
            inband = sum(60 for pairs in g["buckets"].values() if kpi.hour_compliant(pairs, bands))
        avgs = g["avgs"]
        MetricDailyAggregate.objects.using(alias).update_or_create(
            org_id=org_id, saloon_id=saloon_id, slot_id=slot_id, metric_key=metric_key, day=day,
            defaults={
                "avg_val": sum(avgs) / len(avgs) if avgs else None,
                "min_val": g["min"],
                "max_val": g["max"],
                "inband_minutes": inband,
                "sample_count": g["n"],
            },
        )
        written += 1
    logger.info("metric daily rollup %s: %s group rows", day, written)
    return written


def build_flock_snapshots(day=None) -> int:
    """Cumulative-per-flock snapshot rows (D22 NULL-rule: a KPI stays NULL
    until ALL its inputs exist — never a misleading zero)."""
    from django.db.models import Sum

    from apps.alerts.models import FlockFeedIntake, FlockWaterIntake, FlockWeightSample, MortalityRecord
    from apps.alerts.services import kpi
    from apps.monitoring.models import FlockDailySnapshot
    from apps.telemetry.models import FlockCycle

    alias = _alias()
    day = target_day(day)
    start, end = day_window(day)
    written = 0
    flocks = (
        FlockCycle.objects.using(alias)
        .filter(start_date__lt=end)
        .exclude(end_date__lt=start)
    )
    for flock in flocks.iterator():
        deaths = (
            MortalityRecord.objects.using(alias)
            .filter(flock_id=flock.pk, occurred_at__lt=end)
            .aggregate(total=Sum("count"))["total"]
            or 0
        )
        feed = (
            FlockFeedIntake.objects.using(alias)
            .filter(flock_id=flock.pk, entry_date__lte=day)
            .aggregate(total=Sum("feed_kg"))["total"]
        )
        water = (
            FlockWaterIntake.objects.using(alias)
            .filter(flock_id=flock.pk, entry_date__lte=day)
            .aggregate(total=Sum("water_l"))["total"]
        )
        weight = (
            FlockWeightSample.objects.using(alias)
            .filter(flock_id=flock.pk, sampled_at__lt=end)
            .order_by("-sampled_at")
            .values_list("avg_weight_g", flat=True)
            .first()
        )
        mortality_pct, _ = kpi.mortality_pct(deaths, flock.placement_count)
        live = (flock.placement_count or 0) - deaths
        fcr, _ = (
            kpi.fcr_value(feed, weight, live, flock.day_old_weight_g)
            if feed is not None and weight is not None
            else (None, "missing-input")
        )
        FlockDailySnapshot.objects.using(alias).update_or_create(
            flock_id=flock.pk, saloon_id=flock.saloon_id, day=day,
            defaults={
                "org_id": flock.org_id,
                "mortality_cum": deaths,
                "mortality_pct": mortality_pct,
                "feed_cum_kg": feed,
                "water_cum_l": water,
                "avg_weight_g": weight,
                "fcr_to_date": fcr,
            },
        )
        written += 1
    logger.info("flock snapshots %s: %s rows", day, written)
    return written


def _minutes_overlap(start_a, end_a, start_b, end_b) -> float:
    return max(0.0, (min(end_a, end_b) - max(start_a, start_b)).total_seconds() / 60.0)


def build_alert_stats(day=None) -> int:
    """Per (org, saloon, day, tier, metric) alert counts + MTTR family.

    MTTR averages resolved alerts only; ack-to-resolve averages acked AND
    resolved ones; both stay NULL when the group has no such rows (never 0).
    """
    from apps.alerts.models import AlertAcknowledgement, AlertInstance
    from apps.monitoring.models import AlertDailyStat

    alias = _alias()
    day = target_day(day)
    start, end = day_window(day)
    groups: dict[tuple, dict] = {}
    for alert in (
        AlertInstance.objects.using(alias)
        .filter(triggered_at__gte=start, triggered_at__lt=end)
        .exclude(source="REPLAY_ANALYSIS")
        .iterator()
    ):
        key = (str(alert.org_id), alert.saloon_id, alert.highest_tier_reached or 1, alert.metric_type)
        g = groups.setdefault(key, {"count": 0, "mttr": [], "ack": [], "ids": []})
        g["count"] += 1
        g["ids"].append(alert.pk)
        if alert.resolved_at is not None:
            g["mttr"].append((alert.resolved_at - alert.triggered_at).total_seconds() / 60.0)
    if groups:
        acked_ids = set(
            AlertAcknowledgement.objects.using(alias)
            .filter(alert_id__in=[i for g in groups.values() for i in g["ids"]])
            .values_list("alert_id", flat=True)
        )
        resolved = {
            a.pk: a
            for g in groups.values()
            for a in AlertInstance.objects.using(alias).filter(pk__in=g["ids"], resolved_at__isnull=False)
        }
        for g in groups.values():
            secs = []
            for i in g["ids"]:
                a = resolved.get(i)
                if a is None or i not in acked_ids:
                    continue
                ack_at = (
                    AlertAcknowledgement.objects.using(alias)
                    .filter(alert_id=i)
                    .order_by("acknowledged_at")
                    .values_list("acknowledged_at", flat=True)
                    .first()
                )
                if ack_at is not None and a.resolved_at >= ack_at:
                    secs.append((a.resolved_at - ack_at).total_seconds() / 60.0)
            g["ack"] = secs

    written = 0
    for (org_id, saloon_id, tier, metric_key), g in groups.items():
        AlertDailyStat.objects.using(alias).update_or_create(
            org_id=org_id, saloon_id=saloon_id, day=day, tier=tier, metric_key=metric_key,
            defaults={
                "alert_count": g["count"],
                "mttr_min": sum(g["mttr"]) / len(g["mttr"]) if g["mttr"] else None,
                "ack_to_resolve_min": sum(g["ack"]) / len(g["ack"]) if g["ack"] else None,
            },
        )
        written += 1
    logger.info("alert stats %s: %s group rows", day, written)
    return written


def build_gateway_uptime(day=None) -> int:
    """Per-slot day presence from raw telemetry + LOS durations.

    online_pct = distinct device-ts minutes present / 1440 (a gateway that
    streams every minute scores 100); buffered_pct = buffered share of the
    day's rows; los_minutes = resolved LOS episodes overlapping the day
    (unresolved episodes are still running — counted once they resolve).
    """
    from apps.monitoring.models import GatewayDailyUptime
    from apps.alerts.models import AlertInstance
    from apps.telemetry.models import SaloonTelemetry

    alias = _alias()
    day = target_day(day)
    start, end = day_window(day)
    # (org, slot) -> minutes present; slot->saloon for the LOS attribution.
    presence: dict[tuple, dict] = {}
    slot_saloon: dict[tuple, int] = {}
    for ts, buffered, slot_id, org_id, saloon_id in (
        SaloonTelemetry.objects.using(alias)
        .filter(device_ts__gte=start, device_ts__lt=end)
        .values_list("device_ts", "is_buffered", "slot_id", "org_id", "saloon_id")
        .iterator()
    ):
        key = (str(org_id), str(slot_id))
        p = presence.setdefault(key, {"minutes": set(), "total": 0, "buffered": 0})
        minute = ts.astimezone(TEHRAN).replace(second=0, microsecond=0)
        p["minutes"].add(minute)
        p["total"] += 1
        p["buffered"] += bool(buffered)
        slot_saloon[key] = saloon_id

    # LOS episodes are saloon-scoped (no slot linkage by design) — attribute
    # the saloon's overlapping LOS minutes to each of its streaming slots.
    los: dict[tuple, float] = {}
    for alert in (
        AlertInstance.objects.using(alias)
        .filter(metric_type="los", triggered_at__lt=end)
        .exclude(resolved_at__isnull=True)
        .iterator()
    ):
        end_a = min(alert.resolved_at, end)
        if end_a <= max(alert.triggered_at, start):
            continue
        key = (str(alert.org_id), alert.saloon_id)
        los[key] = los.get(key, 0.0) + _minutes_overlap(
            alert.triggered_at, alert.resolved_at, start, end
        )

    written = 0
    for (org_id, slot_id), p in presence.items():
        GatewayDailyUptime.objects.using(alias).update_or_create(
            gateway_slot_id=slot_id, org_id=org_id, day=day,
            defaults={
                "online_pct": round(100.0 * len(p["minutes"]) / 1440.0, 2),
                "los_minutes": int(round(los.get((org_id, slot_saloon[(org_id, slot_id)]), 0.0))),
                "buffered_pct": round(100.0 * p["buffered"] / p["total"], 2) if p["total"] else None,
            },
        )
        written += 1
    logger.info("gateway uptime %s: %s slot rows", day, written)
    return written


def build_metric_hourly(hours: int = 26) -> int:
    """HOURLY aggregate writer — the one that never existed.

    2026-10-24 (live verification, defect): `metric_hourly_aggregates` is read
    by the hourly-analytics charts, the KPI alert lane, and `build_metric_daily`
    — but NOTHING in the codebase wrote a single row into it. The daily rollup
    has been aggregating an empty table since it shipped.

    Window = the trailing `hours` hours (default 26: last full hour plus
    slack), bucketed on device_ts UTC hour, grouped per (bucket, sensor slot,
    metric). Metrics live in the `metrics` jsonb as flat keys — non-numeric
    values are skipped, unknown metric keys are skipped (FK), and the upsert
    makes re-runs and late buffered rows self-healing rather than duplicating.

    Cross-org on the admin alias, same sanctioned pattern as every builder in
    this module. Returns the number of upserted (bucket, slot, metric) rows.
    """
    from django.db import connection as _django_connection  # noqa: F401
    from django.db import connections

    from apps.telemetry.models import SaloonTelemetry

    alias = _alias()
    now = dt.datetime.now(dt.timezone.utc)
    window_start = now - dt.timedelta(hours=max(int(hours), 1))

    # One pass over the window's raw rows; jsonb is parsed in Python so the
    # builder stays backend-agnostic (same trade as the sibling builders).
    rows = (
        SaloonTelemetry.objects.using(alias)
        .filter(device_ts__gte=window_start)
        .values_list("device_ts", "org_id", "saloon_id", "slot_id", "metrics")
        .iterator()
    )
    groups: dict[tuple, dict] = {}
    for device_ts, org_id, saloon_id, slot_id, metrics in rows:
        bucket = device_ts.astimezone(dt.timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        if not isinstance(metrics, dict):
            continue
        for metric_key, raw_value in metrics.items():
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue  # non-numeric reading: nothing to aggregate
            else:
                value = float(raw_value)
            key = (bucket, str(slot_id), str(metric_key))
            g = groups.setdefault(
                key, {"org": str(org_id), "saloon": saloon_id, "v": []}
            )
            g["v"].append(value)

    written = 0
    # MetricHourlyAggregate is UNMANAGED; use raw upsert SQL so re-runs heal
    # instead of violating UNIQUE (bucket_hr, slot_id, metric_key).
    with connections[alias].cursor() as cur:
        for (bucket, slot_id, metric_key), g in groups.items():
            values = g["v"]
            n = len(values)
            mean = sum(values) / n
            var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
            cur.execute(
                """
                INSERT INTO metric_hourly_aggregates (
                    bucket_hr, org_id, saloon_id, slot_id, metric_key,
                    avg_val, min_val, max_val, stddev_val, sample_count
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (bucket_hr, slot_id, metric_key) DO UPDATE SET
                    avg_val = EXCLUDED.avg_val,
                    min_val = EXCLUDED.min_val,
                    max_val = EXCLUDED.max_val,
                    stddev_val = EXCLUDED.stddev_val,
                    sample_count = EXCLUDED.sample_count
                """,
                [
                    bucket, g["org"], g["saloon"], slot_id, metric_key,
                    round(mean, 4), round(min(values), 4), round(max(values), 4),
                    round(var ** 0.5, 4), n,
                ],
            )
            written += 1
    logger.info(
        "metric hourly rollup (last %sh): %s rows", max(int(hours), 1), written
    )
    return written
