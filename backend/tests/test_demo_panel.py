"""Demo panel endpoints — auth gate, publish→row, counters, DLQ tail.

Plain-function style (like other PG integration modules): the publish button
drives the REAL pipeline and its drain must COMMIT for the flow to be honest.
فارسی: تستِ نقاط دید زنده دمو به سبک تابعی تا کامیت‌ها واقعی باشند.
"""

import json
import os
import uuid

import pytest
import redis as redis_lib

# GATE: /demo/api/* exists only when DEBUG or this env var is set.
os.environ.setdefault("MORGHDARI_DEMO_UI", "1")

# ISOLATED scratch logical DB for pipeline state (streams/dedup/groups).
from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL  # noqa: E402

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
from apps.telemetry.consumers import _redis as _consumer_redis  # noqa: E402
from apps.telemetry.models import SaloonTelemetry  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402
from tests.conftest import redis_base_url
from morghdari.subscriber.pipeline import handle_message  # noqa: E402

PASSWORD = "strong-pass-123"


@pytest.fixture(autouse=True)
def _fresh_redis():
    """Wipe the isolated logical DB before every test."""
    _consumer_redis().flushdb()
    yield


def _make_world():
    tag = "pl" + uuid.uuid4().hex[:8]
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
    user = get_user_model().objects.create_user(username=f"pl-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=user, org=org, role="owner", is_active=True)
    return {"org": org, "slot": slot, "user": user}


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
def test_status_requires_authentication(timescale_db):
    world = _make_world()
    response = APIClient().get(reverse("demo-pipeline"))
    assert response.status_code == 401
    del world


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_publish_inserts_row_and_counters_move(timescale_db):
    world = _make_world()
    token = _login(world["user"].username)

    before = _authed_client(token).get(reverse("demo-pipeline")).data["streams"]

    client = _authed_client(token)
    publish = client.post(reverse("demo-simulate"), {"buffered": False}, format="json")
    assert publish.status_code == 200, publish.data
    assert publish.data["outcome"] == "published"

    after = _authed_client(token).get(reverse("demo-pipeline")).data["streams"]
    assert SaloonTelemetry.objects.filter(org_id=world["org"].org_id).count() == 1
    assert after["realtime"] >= before["realtime"]


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_same_window_twice_reports_duplicate(timescale_db):
    world = _make_world()
    token = _login(world["user"].username)
    client = _authed_client(token)

    first = client.post(reverse("demo-simulate"), {"buffered": False}, format="json")
    second = client.post(reverse("demo-simulate"), {"buffered": False}, format="json")
    assert first.data["outcome"] == "published"
    assert second.data["outcome"] == "duplicate-dropped"
    assert SaloonTelemetry.objects.count() == 1


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_dlq_tail_shape(timescale_db):
    world = _make_world()
    token = _login(world["user"].username)
    client = _authed_client(token)
    data = client.get(reverse("demo-dlq")).data
    assert set(data) == {"count", "entries"}
