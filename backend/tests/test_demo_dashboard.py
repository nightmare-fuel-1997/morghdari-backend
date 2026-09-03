"""Demo dashboard endpoints: per-saloon sensor inventory, window stats,
and EMQX reachability. Auth gate, org scoping, health honesty.

Follows test_demo_farm.py conventions: isolated Redis DB, real org/saloon/slot
fixtures, JWT login, and RLS-scoped reads.
"""

import os
import uuid

import pytest
import redis as redis_lib

os.environ.setdefault("MORGHDARI_DEMO_UI", "1")

from tests.conftest import redis_base_url  # noqa: E402

REDIS_URL = redis_base_url() + "/12"  # scratch DB

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
from apps.telemetry.models import SaloonTelemetry  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402

PASSWORD = "strong-pass-123"


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        client.flushdb()
    except redis_lib.RedisError:
        pytest.skip("Redis service is required for demo dashboard integration tests")
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    yield


def _make_world(slot_count=2):
    tag = "ds" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    slots = []
    for i in range(slot_count):
        position = SaloonPosition.objects.create(saloon=saloon, org=org, label=f"pos-{i}")
        slot = SaloonSensorSlot.objects.create(
            slot_id=f"slot-{tag}-{i}", saloon=saloon, org=org, position=position
        )
        GatewaySensorMapping.objects.create(
            gateway_hardware_id=f"hw-{tag}-{i}", pack_id=f"P-{'AB'[i % 2]}", slot=slot, org=org
        )
        slots.append(slot)
    user = get_user_model().objects.create_user(username=f"ds-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=user, org=org, role="owner", is_active=True)
    return {"org": org, "saloon": saloon, "slots": slots, "user": user, "farm": farm}


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


def _add_reading(slot, device_ts, metrics):
    SaloonTelemetry.objects.create(
        org=slot.org, saloon=slot.saloon, slot_id=slot.slot_id,
        device_ts=device_ts, server_received_ts=device_ts,
        metrics=metrics, is_buffered=False,
    )


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_sensors_requires_authentication(timescale_db):
    world = _make_world()
    response = APIClient().get(reverse("demo-saloon-sensors", args=[world["saloon"].pk]))
    assert response.status_code == 401
    del world


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_identity_is_public_and_counts_the_seed_org(timescale_db, _fresh_redis):
    """Login-backdrop identity: NO auth required, and every number comes from
    the real seeded world plus live Redis status keys — never hardcoded."""
    from apps.alerts.services.redis_keys import sensor_status_key

    org = Organization.objects.create(name="Demo Farm")
    province = Province.objects.create(name="p-id")
    city = City.objects.create(province=province, name="c-id")
    farm = Farm.objects.create(org=org, name="f-id", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="s-id")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="pos")
    offline = SaloonSensorSlot.objects.create(
        slot_id="slot-identity-off", saloon=saloon, org=org, position=position
    )
    SaloonSensorSlot.objects.create(
        slot_id="slot-identity-on", saloon=saloon, org=org, position=position
    )
    # One slot live in Redis, one dark → online count must be exactly 1.
    from apps.telemetry import consumers as _consumers
    _consumers._redis_client.set(sensor_status_key(saloon.pk, "slot-identity-on"), "1")

    response = APIClient().get(reverse("demo-identity"))
    assert response.status_code == 200, response.data
    assert response.data["farms"] == 1
    assert response.data["saloons"] == 1
    assert response.data["sensors_online"] == 1


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_identity_404_when_seed_never_ran(timescale_db):
    response = APIClient().get(reverse("demo-identity"))
    assert response.status_code == 404


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_sensors_org_scoping_and_health(timescale_db):
    world = _make_world()
    token = _login(world["user"].username)
    data = _authed_client(token).get(
        reverse("demo-saloon-sensors", args=[world["saloon"].pk])
    ).data
    assert data["saloon_id"] == world["saloon"].pk
    assert data["sensor_count"] == 2
    assert data["health_counts"] == {"online": 0, "stale": 0, "offline": 2}
    sensor = data["sensors"][0]
    assert sensor["pack_id"] in ("P-A", "P-B")
    assert sensor["health"] == "offline"
    assert sensor["last_metrics"] is None

    # Cross-org saloon must 404, not leak.
    other = _make_world()
    response = _authed_client(token).get(
        reverse("demo-saloon-sensors", args=[other["saloon"].pk])
    )
    assert response.status_code == 404
    del other


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_sensors_online_and_breach_flags(timescale_db):
    from datetime import datetime, timezone as dt_tz
    from apps.alerts.services.redis_keys import sensor_status_key

    world = _make_world(slot_count=1)
    slot = world["slots"][0]
    _add_reading(slot, datetime(2026, 8, 30, 12, 0, tzinfo=dt_tz.utc),
                 {"temp": {"avg": 28.0}, "nh3": {"avg": 27.5}})
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.set(sensor_status_key(world["saloon"].pk, slot.slot_id), "live")

    token = _login(world["user"].username)
    data = _authed_client(token).get(
        reverse("demo-saloon-sensors", args=[world["saloon"].pk])
    ).data
    sensor = data["sensors"][0]
    assert sensor["health"] == "online"
    assert sensor["online"] is True
    assert sensor["breaching"] == ["nh3"]
    assert sensor["last_metrics"]["temp"]["avg"] == 28.0


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_stats_window_and_slot_filter(timescale_db):
    from datetime import datetime, timedelta, timezone as dt_tz

    world = _make_world(slot_count=2)
    a, b = world["slots"]
    # base is relative to NOW so the rolling ?window=24h filter always covers it
    base = (datetime.now(dt_tz.utc) - timedelta(hours=2)).replace(second=0, microsecond=0)
    _add_reading(a, base, {"temp": {"avg": 26.0}, "nh3": {"avg": 12.0}})
    _add_reading(a, base + timedelta(minutes=30), {"temp": {"avg": 28.0}, "nh3": {"avg": 14.0}})
    _add_reading(b, base, {"temp": {"avg": 30.0}, "nh3": {"avg": 20.0}})
    # Buffered rows are excluded from stats.
    _add_reading(a, base + timedelta(minutes=45), {"temp": {"avg": 99.0}})
    SaloonTelemetry.objects.filter(slot_id=a.slot_id, device_ts=base + timedelta(minutes=45))         .update(is_buffered=True)

    token = _login(world["user"].username)
    url = reverse("demo-saloon-stats", args=[world["saloon"].pk])
    data = _authed_client(token).get(f"{url}?window=24h").data
    assert data["reading_count"] == 3
    assert data["stats"]["temp"] == {"avg": 28.0, "min": 26.0, "max": 30.0, "count": 3}
    assert data["stats"]["nh3"]["avg"] == round((12.0 + 14.0 + 20.0) / 3, 2)
    assert set(data["per_slot"].keys()) == {a.slot_id, b.slot_id}
    assert data["per_slot"][a.slot_id]["temp"]["count"] == 2

    slot_data = _authed_client(token).get(f"{url}?window=24h&slot_id={a.slot_id}").data
    assert slot_data["reading_count"] == 2
    assert slot_data["stats"]["temp"]["max"] == 28.0

    bad = _authed_client(token).get(f"{url}?window=3x")
    assert bad.status_code == 400

    missing = _authed_client(token).get(
        reverse("demo-saloon-stats", args=[world["saloon"].pk + 999])
    )
    assert missing.status_code == 404


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_emqx_status_shape(timescale_db):
    world = _make_world()
    token = _login(world["user"].username)
    data = _authed_client(token).get(reverse("demo-emqx-status")).data
    assert set(data.keys()) == {
        "host", "mqtt_port", "dashboard_port",
        "mqtt_reachable", "dashboard_reachable", "dashboard_url",
    }
    assert isinstance(data["mqtt_reachable"], bool)
    assert isinstance(data["dashboard_reachable"], bool)
    del world
