"""M4-04 acceptance — breaches become AlertInstance rows on real Postgres.

Plain-function PG modules (memory gotcha: pytest fixtures cannot inject into
TestCase methods); timescale_db fixture replays the hypertable DDL that
--nomigrations hides. House standard ade9e18: NO env mutation — the autouse
fixture injects THE integration Redis client into consumers._redis_client so
drain_stream reads the same /11 lane handle_message published to.

فارسی: پذیرش M4-04 — نقضِ پایدار آستانه ردیفِ هشدار می‌سازد؛ مشاهدهٔ تازهٔ تنها
هیچ چیزی نمی‌سازد (دقیقاً ۹۰۰ ثانیه، نه زودتر).
"""

import json
import time
import uuid

import pytest
import redis as redis_lib

from apps.alerts.models import AlertInstance
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization
from morghdari.subscriber.pipeline import handle_message
from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL  # noqa: E402


def _redis():
    return redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    client = _redis()
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    yield


def _make_world(tag_suffix=None):
    tag = tag_suffix or ("fr" + uuid.uuid4().hex[:8])
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"f-org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot, org=org
    )
    topic = f"{org.org_id}/farms/1/gateways/GW-{tag}/telemetry"

    from apps.alerts.services.redis_keys import thresholds_hash

    rc = _redis()
    rc.hset(thresholds_hash(saloon.id), mapping={
        "temp_max": "30.00",
        "temp_min": "18.00",
        "nh3_enabled": "1",
        "operational_state": "ACTIVE_FLOCK",
    })

    return {"org": org, "saloon": saloon, "slot": slot, "topic": topic,
            "redis": rc, "hw": f"hw-{tag}", "gw": f"GW-{tag}"}


def _ingest(world, device_ts: str, temp_avg: float):
    payload = json.dumps({
        "gateway_hardware_id": world["hw"],
        "gateway_slot_id": world["gw"],
        "schema_version": "2.0",
        "device_ts": device_ts,
        "is_buffered": False,
        "readings": [{"pack_id": "P-A",
                      "metrics": {"temp": {"avg": temp_avg}}}],
    }).encode()
    return handle_message(world["redis"], world["topic"], payload,
                          db_fetch=lambda *a: world["slot"].slot_id)


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_breach_creates_alert_instance(timescale_db):
    """Tier 1 fires ONLY after breach_duration_sec of sustained breach
    (DEMO-M4 §2: 'at exactly elapsed=900 s Tier 1 fires'). The created row
    stamps the LAST observed breaching value; batch_id stays NULL (REALTIME).
    """
    from apps.alerts.services.redis_keys import breach_key
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    now = time.time()

    # Sustained-breach episode opened 901 s ago (duration filter met).
    world["redis"].set(
        breach_key(world["saloon"].id, "temp"),
        json.dumps({"started_at": now - 901.0, "alerted": False,
                    "value": 31.5}),
        ex=900,
    )

    # Latest observation refreshes TTL + value but preserves started_at.
    _ingest(world, "2026-08-26T12:00:00Z", temp_avg=32.0)
    drain_stream("telemetry:realtime", count=10)

    alerts = AlertInstance.objects.filter(org_id=world["org"].org_id)
    assert alerts.count() == 1
    alert = alerts.get()
    assert alert.metric_type == "temp"
    assert float(alert.current_value) == 32.0
    assert alert.current_status == "ACTIVE_TIER_1"
    assert alert.batch_id is None


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_single_observation_below_duration_fires_nothing(timescale_db):
    """First breaching observation opens the episode — NO alert yet."""
    from apps.telemetry.consumers import drain_stream

    world = _make_world()
    _ingest(world, "2026-08-26T12:00:00Z", temp_avg=32.0)
    drain_stream("telemetry:realtime", count=10)

    assert not AlertInstance.objects.filter(org_id=world["org"].org_id).exists()
