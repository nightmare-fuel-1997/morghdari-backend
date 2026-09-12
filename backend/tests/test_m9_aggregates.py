"""v0.9 M9-10 acceptance — batch-1 daily aggregates (docs/data-aggregates-v1.md §2/§4).

Four idempotent upsert writers over Tehran days (+ the backfill command):

- metric_daily_aggregates from hourly rows + the day's threshold bands
  (inband uses kpi.hour_compliant — the SAME definition alerting uses);
- flock_daily_snapshots from mortality/feed/water/weight (D22 NULL-rule:
  a KPI stays NULL until ALL inputs exist; snapshot values equal a fresh
  kpi.* recomputation — the spec's backfill-exactness bar);
- alert_daily_stats counts + MTTR family (resolved-only means, NULL when empty);
- gateway_daily_uptime presence minutes + buffered share + LOS durations.

DDL: portable per-test tables on sqlite (house m7_08 pattern); on Postgres
the `use_migrations` marker materializes the real monitoring/0002 DDL via
conftest (+ the timescale_db hypertables for hourly/raw sources).
"""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.use_migrations

TEHRAN = dt.timezone(dt.timedelta(hours=3, minutes=30))
DAY = dt.date(2026, 9, 10)


@pytest.fixture
def agg_tables(db, timescale_db):
    """Aggregate + source tables per vendor (house m7_08 pattern).

    Postgres: the `use_migrations` marker materialized the real DDL via
    conftest (monitoring/0002) and the timescale_db hypertables — just
    DELETE for isolation. sqlite: portable CREATE TABLE IF NOT EXISTS
    (no RLS there) + DELETE.
    """
    tables = (
        "metric_daily_aggregates", "flock_daily_snapshots", "alert_daily_stats",
        "gateway_daily_uptime", "saloon_telemetry", "metric_hourly_aggregates",
    )
    with connection.cursor() as cursor:
        if connection.vendor != "postgresql":
            for stmt in _SQLITE_DDL:
                cursor.execute(stmt)
        for table in tables:
            try:
                cursor.execute(f"DELETE FROM {table}")
            except Exception:  # noqa: BLE001 — table absent on this vendor
                pass
    yield


_SQLITE_DDL = [
        "CREATE TABLE IF NOT EXISTS metric_daily_aggregates ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, org_id CHAR(32) NOT NULL, saloon_id INTEGER NOT NULL, "
        "slot_id VARCHAR(50) NOT NULL, metric_key VARCHAR(50) NOT NULL, day DATE NOT NULL, "
        "avg_val NUMERIC(10,4) NULL, min_val NUMERIC(10,4) NULL, max_val NUMERIC(10,4) NULL, "
        "inband_minutes INTEGER NOT NULL DEFAULT 0, sample_count INTEGER NOT NULL DEFAULT 0, "
        "CONSTRAINT metric_daily_aggregates_grain UNIQUE (org_id, saloon_id, slot_id, metric_key, day))",
        "CREATE TABLE IF NOT EXISTS flock_daily_snapshots ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, org_id CHAR(32) NOT NULL, flock_id INTEGER NOT NULL, saloon_id INTEGER NOT NULL, "
        "day DATE NOT NULL, mortality_cum INTEGER NOT NULL DEFAULT 0, mortality_pct NUMERIC(5,2) NULL, "
        "feed_cum_kg NUMERIC(10,2) NULL, water_cum_l NUMERIC(10,2) NULL, avg_weight_g NUMERIC(8,2) NULL, "
        "fcr_to_date NUMERIC(10,3) NULL, "
        "CONSTRAINT flock_daily_snapshots_grain UNIQUE (flock_id, saloon_id, day))",
        "CREATE TABLE IF NOT EXISTS alert_daily_stats ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, org_id CHAR(32) NOT NULL, saloon_id INTEGER NOT NULL, "
        "day DATE NOT NULL, tier INTEGER NOT NULL, metric_key VARCHAR(20) NOT NULL, "
        "alert_count INTEGER NOT NULL DEFAULT 0, mttr_min NUMERIC(10,2) NULL, "
        "ack_to_resolve_min NUMERIC(10,2) NULL, "
        "CONSTRAINT alert_daily_stats_grain UNIQUE (org_id, saloon_id, day, tier, metric_key))",
        "CREATE TABLE IF NOT EXISTS gateway_daily_uptime ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_slot_id VARCHAR(50) NOT NULL, "
        "org_id CHAR(32) NOT NULL, day DATE NOT NULL, online_pct NUMERIC(5,2) NULL, "
        "los_minutes INTEGER NOT NULL DEFAULT 0, buffered_pct NUMERIC(5,2) NULL, "
        "CONSTRAINT gateway_daily_uptime_grain UNIQUE (gateway_slot_id, org_id, day))",
        "CREATE TABLE IF NOT EXISTS saloon_telemetry ("
        "server_received_ts TIMESTAMP NOT NULL PRIMARY KEY, device_ts TIMESTAMP NOT NULL, "
        "org_id VARCHAR(32) NOT NULL, saloon_id INTEGER NOT NULL, slot_id VARCHAR(50) NOT NULL, "
        "is_buffered BOOLEAN, clock_drift BOOLEAN, metrics TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS metric_hourly_aggregates ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, bucket_hr TIMESTAMP NOT NULL, org_id VARCHAR(32) NOT NULL, "
        "saloon_id INTEGER NOT NULL, slot_id VARCHAR(50) NOT NULL, metric_key VARCHAR(20) NOT NULL, "
        "avg_val NUMERIC(10,4) NULL, min_val NUMERIC(10,4) NULL, max_val NUMERIC(10,4) NULL, "
        "stddev_val NUMERIC(10,4) NULL, sample_count INTEGER NOT NULL DEFAULT 0, "
        "high_variance BOOLEAN NULL)",
    ]


@pytest.fixture
def world(db, agg_tables):
    from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Farm, Saloon
    from apps.geography.models import City, Province
    from apps.telemetry.models import FlockCycle, SensorMetricType
    from apps.tenants.models import Organization

    org = Organization.objects.create(name="AGG-ORG")
    user = get_user_model().objects.create_user(username="agg-eng", password="x")
    province = Province.objects.create(name="P-AGG")
    city = City.objects.create(province=province, name="C-AGG")
    farm = Farm.objects.create(org=org, name="F-AGG", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="S-AGG")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="North")
    slot = SaloonSensorSlot.objects.create(
        slot_id="AGG-PACK-A", saloon=saloon, org=org, position=position
    )
    SensorMetricType.objects.update_or_create(
        metric_key="temp", defaults={"display_name": "Temperature", "unit": "C"}
    )
    flock = FlockCycle.objects.create(
        org=org, saloon=saloon, breed_type="ross308",
        start_date=dt.datetime(2026, 8, 21, 6, 0, tzinfo=dt.timezone.utc),
        placement_count=100, day_old_weight_g=40,
    )
    template = LifecycleTemplate.objects.create(org=org, is_default=True, status="PUBLISHED")
    LifecycleThresholdRule.objects.create(
        template=template, day_from=0, day_to=400, metric="temp", min_value=18, max_value=30
    )
    return locals()


def _tehran(dt_utc):
    return dt_utc.astimezone(TEHRAN)


def _insert_hourly(org_id, saloon_id, slot_id, metric_key, bucket_hr, avg, min_v, max_v, samples):
    """Explicit-column INSERT (house m6_01 pattern): the PG hypertable's
    high_variance is GENERATED ALWAYS, so ORM create dies there."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO metric_hourly_aggregates "
            "(bucket_hr, org_id, saloon_id, slot_id, metric_key, avg_val, min_val, max_val, sample_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [bucket_hr, str(org_id), saloon_id, slot_id, metric_key, avg, min_v, max_v, samples],
        )


@pytest.mark.django_db(transaction=False)
def test_metric_daily_rollup_and_idempotent(world):
    from apps.monitoring.models import MetricDailyAggregate
    from apps.monitoring.services_aggregates import build_metric_daily
    from apps.telemetry.models import SensorMetricType

    w = world
    SensorMetricType.objects.update_or_create(
        metric_key="temp", defaults={"display_name": "Temperature", "unit": "C"}
    )
    for hour, avg in ((0, 20.0), (1, 22.0), (2, 24.0)):
        _insert_hourly(
            w["org"].org_id, w["saloon"].pk, w["slot"].slot_id, "temp",
            dt.datetime(2026, 9, 10, hour, 0, tzinfo=dt.timezone.utc), avg, 18.0, 26.0, 4,
        )
    assert build_metric_daily(DAY) == 1
    row = MetricDailyAggregate.objects.get()
    assert float(row.avg_val) == 22.0
    assert float(row.min_val) == 18.0
    assert float(row.max_val) == 26.0
    assert row.sample_count == 3
    assert row.inband_minutes == 180  # all three hourly avgs inside 18–30
    assert build_metric_daily(DAY) == 1
    assert MetricDailyAggregate.objects.count() == 1  # upsert, never duplicate


@pytest.mark.django_db(transaction=False)
def test_metric_daily_no_flock_zero_inband(world):
    from apps.farms.models import Saloon
    from apps.monitoring.models import MetricDailyAggregate
    from apps.monitoring.services_aggregates import build_metric_daily
    from apps.telemetry.models import MetricHourlyAggregate, SensorMetricType

    w = world
    bare = Saloon.objects.create(org=w["org"], farm=w["farm"], name="S-BARE")
    SensorMetricType.objects.update_or_create(
        metric_key="temp", defaults={"display_name": "Temperature", "unit": "C"}
    )
    _insert_hourly(
        w["org"].org_id, bare.pk, "BARE-PACK", "temp",
        dt.datetime(2026, 9, 10, 5, 0, tzinfo=dt.timezone.utc), 25.0, 25.0, 25.0, 2,
    )
    build_metric_daily(DAY)
    row = MetricDailyAggregate.objects.get(saloon_id=bare.pk)
    assert row.inband_minutes == 0  # no flock, no bands — honest, not zero-percent
    assert float(row.avg_val) == 25.0


@pytest.mark.django_db(transaction=False)
def test_flock_snapshot_numbers_match_kpi_recompute(world):
    import datetime as _dt

    from apps.alerts.models import FlockFeedIntake, FlockWaterIntake, FlockWeightSample, MortalityRecord
    from apps.alerts.services import kpi
    from apps.monitoring.models import FlockDailySnapshot
    from apps.monitoring.services_aggregates import build_flock_snapshots

    w = world
    flock, saloon, org, user = w["flock"], w["saloon"], w["org"], w["user"]
    MortalityRecord.objects.create(
        org=org, flock=flock, saloon=saloon, recorded_by=user, count=5,
        occurred_at=_dt.datetime(2026, 9, 9, 10, 0, tzinfo=_dt.timezone.utc),
    )
    MortalityRecord.objects.create(
        org=org, flock=flock, saloon=saloon, recorded_by=user, count=3,
        occurred_at=_dt.datetime(2026, 9, 10, 10, 0, tzinfo=_dt.timezone.utc),
    )
    FlockFeedIntake.objects.create(
        org=org, flock=flock, saloon=saloon, entry_date=_dt.date(2026, 9, 9), feed_kg=10, recorded_by=user
    )
    FlockFeedIntake.objects.create(
        org=org, flock=flock, saloon=saloon, entry_date=_dt.date(2026, 9, 10), feed_kg=12, recorded_by=user
    )
    FlockWaterIntake.objects.create(
        org=org, flock=flock, saloon=saloon, entry_date=_dt.date(2026, 9, 10), water_l=40, recorded_by=user
    )
    FlockWeightSample.objects.create(
        org=org, flock=flock, saloon=saloon, sample_size=50, avg_weight_g=1500,
        sampled_at=_dt.datetime(2026, 9, 10, 8, 0, tzinfo=_dt.timezone.utc), recorded_by=user,
    )
    assert build_flock_snapshots(DAY) == 1
    snap = FlockDailySnapshot.objects.get()
    assert snap.mortality_cum == 8
    # Spec bar: snapshot values EQUAL a fresh kpi.* recomputation on the same inputs.
    expect_pct, _ = kpi.mortality_pct(8, 100)
    assert float(snap.mortality_pct) == expect_pct == 8.0
    assert float(snap.feed_cum_kg) == 22.0
    expect_fcr, _ = kpi.fcr_value(22.0, 1500.0, 92, 40)
    assert abs(float(snap.fcr_to_date) - expect_fcr) < 0.001
    assert build_flock_snapshots(DAY) == 1
    assert FlockDailySnapshot.objects.count() == 1


@pytest.mark.django_db(transaction=False)
def test_flock_snapshot_null_fcr_without_weight(world):
    from apps.monitoring.models import FlockDailySnapshot
    from apps.monitoring.services_aggregates import build_flock_snapshots

    build_flock_snapshots(DAY)
    snap = FlockDailySnapshot.objects.get()
    assert snap.fcr_to_date is None  # D22 NULL-rule: no weight sample, never 0
    assert snap.feed_cum_kg is None
    assert float(snap.mortality_pct) == 0.0  # zero deaths IS a true zero


@pytest.mark.django_db(transaction=False)
def test_alert_stats_counts_and_means(world):
    import datetime as _dt

    from apps.alerts.models import AlertAcknowledgement, AlertInstance
    from apps.monitoring.models import AlertDailyStat
    from apps.monitoring.services_aggregates import build_alert_stats

    w = world
    base = _dt.datetime(2026, 9, 10, 6, 0, tzinfo=_dt.timezone.utc)
    a1 = AlertInstance.objects.create(
        org=w["org"], farm=w["farm"], saloon=w["saloon"], metric_type="temp",
        current_status="RESOLVED", triggered_at=base,
        resolved_at=base + _dt.timedelta(minutes=60), highest_tier_reached=1,
    )
    AlertAcknowledgement.objects.create(
        alert=a1, org=w["org"], acknowledged_by=w["user"],
        acknowledged_at=base + _dt.timedelta(minutes=20),
    )
    AlertInstance.objects.create(
        org=w["org"], farm=w["farm"], saloon=w["saloon"], metric_type="temp",
        current_status="ACTIVE_TIER_1", triggered_at=base + _dt.timedelta(minutes=90),
        highest_tier_reached=1,
    )
    assert build_alert_stats(DAY) == 1
    row = AlertDailyStat.objects.get()
    assert (row.tier, row.metric_key, row.alert_count) == (1, "temp", 2)
    assert float(row.mttr_min) == 60.0  # resolved-only mean
    assert float(row.ack_to_resolve_min) == 40.0  # acked-AND-resolved only


@pytest.mark.django_db(transaction=False)
def test_gateway_uptime_presence_buffered_and_los(world):
    import datetime as _dt

    from apps.alerts.models import AlertInstance
    from apps.monitoring.models import GatewayDailyUptime
    from apps.monitoring.services_aggregates import build_gateway_uptime
    from apps.telemetry.models import SaloonTelemetry

    w = world
    base = _dt.datetime(2026, 9, 10, 6, 0, tzinfo=_dt.timezone.utc)
    for minute, buffered in ((0, False), (1, False), (2, True)):
        ts = base + _dt.timedelta(minutes=minute)
        SaloonTelemetry.objects.create(
            server_received_ts=ts, device_ts=ts, org=w["org"], saloon=w["saloon"],
            slot=w["slot"], is_buffered=buffered, metrics={"temp": 25.0},
        )
    AlertInstance.objects.create(
        org=w["org"], farm=w["farm"], saloon=w["saloon"], metric_type="los",
        current_status="RESOLVED", triggered_at=base,
        resolved_at=base + _dt.timedelta(minutes=30), highest_tier_reached=1,
    )
    assert build_gateway_uptime(DAY) == 1
    row = GatewayDailyUptime.objects.get()
    assert float(row.online_pct) == round(100.0 * 3 / 1440.0, 2)
    assert abs(float(row.buffered_pct) - 100.0 / 3.0) < 0.01
    assert row.los_minutes == 30


@pytest.mark.django_db(transaction=False)
def test_backfill_command_covers_range(world):
    from django.core.management import call_command

    from apps.monitoring.models import FlockDailySnapshot
    from apps.telemetry.models import FlockCycle

    FlockCycle.objects.filter(pk=world["flock"].pk).update(
        start_date=dt.datetime(2026, 9, 8, 6, 0, tzinfo=dt.timezone.utc)
    )
    call_command("backfill_aggregates", "--from", "2026-09-09", "--to", "2026-09-10")
    days = sorted(
        FlockDailySnapshot.objects.filter(flock_id=world["flock"].pk).values_list("day", flat=True)
    )
    assert [d.isoformat() for d in days] == ["2026-09-09", "2026-09-10"]
