"""Pack-LWT bridge — the dead-pack lane on the boss board.

The simulator's per-pack MQTT clients carry a Last Will: on unclean death the
broker flips the retained farm/{farm}/gateways/{gw}/status topic to 'offline'
(and the sim writes it itself on clean shutdown). The subscriber mirrors that
retained state into sensor:lwt:{saloon}:{slot} (NO TTL — the retained MQTT
state is the durable truth), and farm_topology surfaces pack_alive per slot
plus dead_packs per farm from the SAME keys.

فارسی: پلِ وضعیت پک — مرگِ بی‌سر و صدای یک پک روی داشبورد مزرعه فوراً دیده شود.
"""

import json
import os
import uuid

import pytest
import redis as redis_lib

# GATE: /demo/api/* exists only when DEBUG or this env var is set.
os.environ.setdefault("MORGHDARI_DEMO_UI", "1")

from tests.conftest import redis_base_url  # noqa: E402

REDIS_URL = redis_base_url() + "/13"  # scratch DB

from django.contrib.auth import get_user_model  # noqa: E402
from django.urls import reverse  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import OrgMember  # noqa: E402
from apps.alerts.services.redis_keys import pack_lwt_key  # noqa: E402
from apps.devices.models import (  # noqa: E402
    GatewaySensorMapping,
    SaloonPosition,
    SaloonSensorSlot,
)
from apps.farms.models import Farm, Saloon  # noqa: E402
from apps.geography.models import City, Province  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402
from morghdari.subscriber.main import _handle_status  # noqa: E402

PASSWORD = "strong-pass-123"


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
    user = get_user_model().objects.create_user(username=f"lwt-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=user, org=org, role="owner", is_active=True)
    return {"org": org, "saloon": saloon, "slot": slot, "user": user, "farm": farm}


def _login(username):
    client = APIClient()
    response = client.post(
        reverse("accounts:auth-login"),
        {"username": username, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.data
    return response.data["access_token"]


def _authed_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _status_payload(status):
    return json.dumps({
        "status": status, "schema_version": "2.0",
        "gateway_hardware_id": "hw", "gateway_slot_id": "slot", "pack_id": "P-A",
    }).encode()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_subscriber_mirrors_retained_offline_into_lwt_key(timescale_db, _fresh_redis):
    """The bridge stores the Last-Will 'offline' under the slot's lwt key —
    that key is what the boss board reads, so a dead pack is visible with no
    waiting for the sensor-status TTL."""
    world = _make_world()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)

    topic = (
        f"{world['org'].org_id}/farms/{world['farm'].pk}"
        f"/gateways/{world['slot'].slot_id}/status"
    )
    _handle_status(client, topic, _status_payload("offline"))

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
    _handle_status(client, topic, _status_payload("offline"))
    assert client.keys("sensor:lwt:*") == []

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
    _handle_status(client, "org/farms/1/gateways", _status_payload("offline"))
    assert client.keys("sensor:lwt:*") == []
    del world


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_topology_surfaces_dead_pack_and_recovers(timescale_db, _fresh_redis):
    """The board: absent lwt key = alive (no death signal); retained
    'offline' = dead NOW at the farm level; retained 'online' = recovered."""
    world = _make_world()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    token = _login(world["user"].username)

    # no lwt key yet → the pack is alive, farm shows zero dead
    data = _authed_client(token).get(reverse("demo-farm")).data
    slot = data["saloons"][0]["slots"][0]
    assert slot["lwt"] is None
    assert slot["pack_alive"] is True
    assert data["farms"][0]["dead_packs"] == 0

    # the bridge mirrored the will → dead immediately (even though no
    # telemetry was ever pushed, so no sensor-status key exists at all)
    key = pack_lwt_key(world["saloon"].pk, world["slot"].slot_id)
    client.set(key, "offline")
    data = _authed_client(token).get(reverse("demo-farm")).data
    slot = data["saloons"][0]["slots"][0]
    assert slot["lwt"] == "offline"
    assert slot["pack_alive"] is False
    assert data["farms"][0]["dead_packs"] == 1

    # the pack is back (retained 'online' on reconnect) → the board clears
    client.set(key, "online")
    data = _authed_client(token).get(reverse("demo-farm")).data
    slot = data["saloons"][0]["slots"][0]
    assert slot["lwt"] == "online"
    assert slot["pack_alive"] is True
    assert data["farms"][0]["dead_packs"] == 0
    del world