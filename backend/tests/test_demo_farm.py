"""/demo/api/farm/ — auth gate, caller-org topology, liveness + latest reading.

Follows test_demo_panel.py conventions: the publish path drives the REAL
pipeline (handle_message -> drain), and liveness keys are the SAME Redis keys
the LOS watchdog uses, so "online" on the dashboard means what the alert
engine means.
فارسی: تستِ نقشهٔ توپولوژی زندهٔ دمو — دروازهٔ توکن، محدودهٔ سازمان، وضعیت زنده.
"""

import json
import os
import uuid

import pytest
import redis as redis_lib

# GATE: /demo/api/* exists only when DEBUG or this env var is set.
os.environ.setdefault("MORGHDARI_DEMO_UI", "1")

from tests.conftest import redis_base_url  # noqa: E402

REDIS_URL = redis_base_url() + "/11"  # scratch DB

from django.contrib.auth import get_user_model  # noqa: E402
from django.urls import reverse  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import OrgMember  # noqa: E402
from apps.devices.models import (  # noqa: E402
    GatewaySensorMapping,
    SaloonPosition,
    SaloonSensorSlot,
)
from apps.farms.models import Farm, Saloon  # noqa: E402
from apps.geography.models import City, Province  # noqa: E402
from apps.telemetry.consumers import STREAM_REALTIME  # noqa: E402
from apps.telemetry.consumers import _redis as _consumer_redis  # noqa: E402
from apps.telemetry.consumers import drain_stream  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402
from morghdari.subscriber.pipeline import handle_message  # noqa: E402

PASSWORD = "strong-pass-123"


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    """Wipe the isolated logical DB and bind consumers to it."""
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    yield


def _make_world():
    tag = "df" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position
    )
    hardware_id = f"hw-{tag}"
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=hardware_id, pack_id="P-A", slot=slot, org=org
    )
    user = get_user_model().objects.create_user(username=f"df-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=user, org=org, role="owner", is_active=True)
    return {
        "org": org, "saloon": saloon, "slot": slot, "user": user,
        "farm": farm, "hardware_id": hardware_id,
    }


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


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_requires_authentication(timescale_db):
    world = _make_world()
    response = APIClient().get(reverse("demo-farm"))
    assert response.status_code == 401
    del world


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_topology_shape_and_org_scoping(timescale_db):
    world = _make_world()
    # a second org's saloon must NOT leak into the caller's topology
    tag2 = "df2" + uuid.uuid4().hex[:8]
    other_org = Organization.objects.create(name=f"other-{tag2}")
    other_farm = Farm.objects.create(
        org=other_org, name=f"of-{tag2}",
        province=Province.objects.first(), city=City.objects.first(),
    )
    Saloon.objects.create(org=other_org, farm=other_farm, name=f"os-{tag2}")

    token = _login(world["user"].username)
    data = _authed_client(token).get(reverse("demo-farm")).data
    assert data["org_id"] == str(world["org"].org_id)
    assert len(data["saloons"]) == 1
    saloon = data["saloons"][0]
    assert saloon["id"] == world["saloon"].pk
    assert saloon["name"] == world["saloon"].name
    assert saloon["farm_id"] == world["farm"].pk
    assert saloon["farm_name"] == world["farm"].name
    assert saloon["flock"] is None  # no open flock seeded in this world
    assert len(saloon["slots"]) == 1
    slot = saloon["slots"][0]
    assert slot["slot_id"] == world["slot"].slot_id
    assert slot["pack_id"] == "P-A"
    assert slot["gateway_hardware_id"] == world["hardware_id"]
    assert slot["online"] is False
    assert slot["last_metrics"] is None

    # boss-board grouping: the caller's farm is present, the other org's is not
    assert len(data["farms"]) == 1
    farm_group = data["farms"][0]
    assert farm_group["name"] == world["farm"].name
    assert farm_group["saloon_count"] == 1
    assert farm_group["online_slots"] == 0
    assert farm_group["saloons"][0]["id"] == world["saloon"].pk


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_liveness_and_latest_after_real_publish(timescale_db):
    world = _make_world()
    token = _login(world["user"].username)
    client = _authed_client(token)

    payload = json.dumps({
        "gateway_hardware_id": f"hw-{world['slot'].slot_id[-8:]}",
        "gateway_slot_id": world["slot"].slot_id,
        "schema_version": "2.0",
        "device_ts": "2026-08-30T12:00:00Z",
        "is_buffered": False,
        "readings": [{
            "pack_id": "P-A",
            "metrics": {"temp": {"avg": 28.0, "min": 27.0, "max": 29.0},
                        "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0}},
        }],
    }).encode()
    topic = f"{world['org'].org_id}/farms/{world['farm'].pk}/gateways/{world['slot'].slot_id}/telemetry"
    result = handle_message(
        _consumer_redis(), topic, payload,
        db_fetch=lambda *a: world["slot"].pk,
    )
    assert result is None
    drained = drain_stream(STREAM_REALTIME, count=50)
    assert drained >= 1

    # An open flock makes the boss board's summary line truthful.
    from datetime import datetime, timedelta, timezone as dt_tz
    from apps.telemetry.models import FlockCycle
    FlockCycle.objects.create(
        org=world["org"], saloon=world["saloon"], breed_type="ross",
        start_date=datetime.now(tz=dt_tz.utc) - timedelta(days=19),
        placement_count=12400, day_old_weight_g=720,
    )

    data = _authed_client(token).get(reverse("demo-farm")).data
    slot = data["saloons"][0]["slots"][0]
    assert slot["online"] is True
    assert slot["last_metrics"]["temp"]["avg"] == 28.0
    assert slot["last_device_ts"] is not None

    flock = data["saloons"][0]["flock"]
    assert flock is not None
    assert flock["breed_type"] == "ross"
    assert flock["placement_count"] == 12400
    assert flock["day"] == 20
    # The flock dial draws against the seeded lifecycle span (no template in
    # this test world → the honest industry fallback).
    assert flock["cycle_days"] == 45