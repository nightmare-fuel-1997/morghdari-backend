"""Pack-LWT bridge — the subscriber lane that mirrors a dead pack.

The simulator's per-pack MQTT clients carry a Last Will: on unclean death the
broker flips the retained farm/{farm}/gateways/{gw}/status topic to 'offline'
(and the sim writes it itself on clean shutdown). The subscriber mirrors that
retained state into sensor:lwt:{saloon}:{slot} (NO TTL — the retained MQTT
state is the durable truth).

These tests pin the WRITE side only. The read side was the demo board's
farm_topology endpoint, removed 2026-09-11 with the demo console, so nothing
in the contract API surfaces these keys today (v0.8 planning note).

فارسی: این تست‌ها فقط نوشتنِ کلید وضعیت پک را قفل می‌کنند؛ خوانندهٔ آن با
کنsole دمو حذف شد.
"""

import json
import uuid

import pytest
import redis as redis_lib

from tests.conftest import redis_base_url  # noqa: E402

REDIS_URL = redis_base_url() + "/13"  # scratch DB

from apps.devices.models import (  # noqa: E402
    GatewaySensorMapping,
    SaloonPosition,
    SaloonSensorSlot,
)
from apps.farms.models import Farm, Saloon  # noqa: E402
from apps.geography.models import City, Province  # noqa: E402
from apps.alerts.services.redis_keys import pack_lwt_key  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402
from morghdari.subscriber.main import _handle_status  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    """Wipe the isolated logical DB and bind consumers to it."""
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    yield


def _make_world():
    tag = "lwt" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot, org=org
    )
    return {"org": org, "saloon": saloon, "slot": slot, "farm": farm}


def _status_payload(status, slot_id):
    """MQTT Topic Map §1.2: statuses are ONLINE/OFFLINE and the payload mirrors
    the topic's gateway_slot_id — the subscriber treats a mismatch as an
    identity fault (same rule as the telemetry lane)."""
    return json.dumps({
        "status": status, "schema_version": "2.0",
        "gateway_hardware_id": "hw", "gateway_slot_id": slot_id,
    }).encode()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_subscriber_mirrors_retained_offline_into_lwt_key(timescale_db, _fresh_redis):
    """The bridge stores the Last-Will 'offline' under the slot's lwt key —
    that key is the durable 'this pack died' signal, visible with no waiting
    for the sensor-status TTL."""
    world = _make_world()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)

    topic = (
        f"{world['org'].org_id}/farms/{world['farm'].pk}"
        f"/gateways/{world['slot'].slot_id}/status"
    )
    _handle_status(client, topic, _status_payload("OFFLINE", world["slot"].slot_id))

    assert client.get(pack_lwt_key(world["saloon"].pk, world["slot"].slot_id)) == "offline"
    del world


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_subscriber_ignores_unknown_slots_and_bad_payloads(timescale_db, _fresh_redis):
    """The bridge never crashes: unknown slots, malformed JSON and unknown
    status words leave the lwt keys untouched."""
    world = _make_world()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)

    # unknown slot → no key, no crash
    topic = f"{world['org'].org_id}/farms/1/gateways/ghost-slot/status"
    _handle_status(client, topic, _status_payload("OFFLINE", "ghost-slot"))
    assert client.keys("sensor:lwt:*") == []
    # A gateway that publishes exists, so the gateway-level key IS written —
    # only the (unknown) sensor mirror is skipped. See gateway_lwt_key.
    assert client.keys("gateway:lwt:*")

    # malformed payload → ignored
    good_topic = (
        f"{world['org'].org_id}/farms/{world['farm'].pk}"
        f"/gateways/{world['slot'].slot_id}/status"
    )
    _handle_status(client, good_topic, b"not-json")
    assert client.keys("sensor:lwt:*") == []

    # unknown status word → last known state stays untouched
    _handle_status(client, good_topic, b'{"status": "suspended"}')
    assert client.keys("sensor:lwt:*") == []

    # badly-shaped topic → ignored
    _handle_status(client, "org/farms/1/gateways", _status_payload("OFFLINE", "slot"))
    assert client.keys("sensor:lwt:*") == []

    # identity fault → the topic's gateway segment must equal the payload's
    _handle_status(client, good_topic, _status_payload("OFFLINE", "someone-else"))
    assert client.keys("sensor:lwt:*") == []
    del world
