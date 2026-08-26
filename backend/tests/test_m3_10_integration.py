"""M3-10 acceptance — storm lane separation + bounded streams (WIP).

STATUS (2026-08-25, end of session): lane/dedup ANOMALY under investigation.
Observed on sequence [3 buffered oldest-first, then 2 live]: XLEN showed
realtime=3 / historical=3 (expected 2/3) BEFORE any drain — one publish
mis-laned or an extra entry appeared. Single-flow probe (1 entry) behaves
perfectly. Next debugging steps:
  1. log lane choice + dedup-key NX result per publish inside
     pipeline.handle_message behind M3DEBUG env;
  2. check whether the FIRST backlog window's dedup key collides with the
     previous test's keys (org uuid differs — should be impossible);
  3. verify replay:pending INCR isn't cross-wiring the lane variable.
Everything else in this module (world builder, ingest helper, drain-all) is
final and reused by the fix.

فارسی: وضعیت فعلی — ناهنجاریِ لِین در توالیِ طوفان در حال بررسی است؛ بقیهٔ
زیرساختِ تست نهایی و قابل استفاده است.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import redis as redis_lib
from django.db import connection

from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry.consumers import STREAM_HISTORICAL, STREAM_REALTIME, drain_stream
from apps.telemetry.models import SaloonTelemetry
from apps.tenants.models import Organization
from morghdari.subscriber.pipeline import handle_message

REDIS_URL = "redis://:dev_redis_password@localhost:6379/12"  # scratch DB
os_set = __import__("os").environ.__setitem__
os_set("REDIS_URL", REDIS_URL)


def _redis():
    return redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)


@pytest.fixture(autouse=True)
def _fresh_redis():
    _redis().flushdb()
    yield


def _make_world(tag: str):
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"storm-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot, org=org
    )
    return {
        "org": org,
        "slot": slot,
        "topic": f"{org.org_id}/farms/1/gateways/GW-{tag}/telemetry",
        "hw": f"hw-{tag}",
        "gw": f"GW-{tag}",
    }


def _ingest_window(world, device_ts_iso: str, buffered: bool):
    payload = json.dumps({
        "gateway_hardware_id": world["hw"],
        "gateway_slot_id": world["gw"],
        "schema_version": "2.0",
        "device_ts": device_ts_iso,
        "is_buffered": buffered,
        "readings": [{"pack_id": "P-A",
                      "metrics": {"temp": {"avg": 28.0 + (hash(device_ts_iso) % 30) / 10}}}],
    }).encode()
    assert handle_message(
        _redis(), world["topic"], payload, db_fetch=lambda *a: world["slot"].slot_id
    ) is None


def _drain_all(stream):
    total = 0
    while True:
        n = drain_stream(stream, count=100)
        total += n
        if n < 100:
            return total


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_storm_lanes_stay_separate_and_bounded(timescale_db):
    world = _make_world("st" + uuid.uuid4().hex[:8])
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    for i in range(240, 0, -1):  # oldest backlog window first (4h outage)
        ts = (base - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _ingest_window(world, ts, buffered=True)
    for i in range(20):
        ts = (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _ingest_window(world, ts, buffered=False)

    client = _redis()
    # Lane separation BEFORE any drain: nothing crossed streams.
    assert client.xlen(STREAM_REALTIME) == 20
    assert client.xlen(STREAM_HISTORICAL) == 240

    # Live drains FIRST and completely — the heartbeat is instant.
    assert drain_stream(STREAM_REALTIME, count=50) == 20
    assert SaloonTelemetry.objects.filter(
        org_id=world["org"].org_id, is_buffered=False).count() == 20

    # Historical drains the whole backlog without touching live rows again.
    assert _drain_all(STREAM_HISTORICAL) == 240
    total = SaloonTelemetry.objects.filter(org_id=world["org"].org_id)
    assert total.count() == 260
    assert total.filter(is_buffered=True).count() == 240

    # Bounded streams: trim ran on every publish; nothing runaway.
    assert client.xlen(STREAM_REALTIME) <= 100_000
    assert client.xlen(STREAM_HISTORICAL) <= 100_000

    # Hypertable reality: TimescaleDB chunking machinery exists for this table.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'saloon_telemetry'"
        )
        assert cursor.fetchone()[0] == 1


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_dedup_holds_across_a_full_backlog_replay(timescale_db):
    world = _make_world("dd" + uuid.uuid4().hex[:8])
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    windows = [(base - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
               for m in range(60)]

    for ts in windows:  # first broadcast of the outage window set
        _ingest_window(world, ts, buffered=True)
    for ts in windows:  # FULL replay of identical windows (EMQX redelivery)
        _ingest_window(world, ts, buffered=True)

    assert _drain_all(STREAM_HISTORICAL) == 60
    assert SaloonTelemetry.objects.filter(org_id=world["org"].org_id).count() == 60
