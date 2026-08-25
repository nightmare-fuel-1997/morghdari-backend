"""M3-09 acceptance — the loop closes: curl shows YOUR simulated row.

WHY: first end-to-end moment — a reading that entered via MQTT exits through
the API, RLS-scoped to the caller's org (cross-org token ⇒ HTTP 200 with an
EMPTY list — C-14 family), anonymous ⇒ 401, contract filters work, and the
DB-GENERATED buffered_delay_mins is read back via annotation.
فارسی: پذیرش M3-09 — خواندن تله‌متری از API با فیلترها؛ دسترسی بین‌سازمانی
نتیجهٔ خالی با ۲۰۰ می‌دهد و بی‌توکن ۴۰۱.
"""

import json
import os
import uuid

import pytest
import redis as redis_lib
from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry.consumers import drain_stream
from apps.tenants.models import Organization
from morghdari.subscriber.pipeline import handle_message

PASSWORD = "strong-pass-123"
REDIS_URL = "redis://:dev_redis_password@localhost:6379/13"  # scratch DB
os.environ["REDIS_URL"] = REDIS_URL  # consumers must talk to the SAME db


def _redis():
    return redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)


@pytest.fixture(autouse=True)
def _fresh_redis():
    """Wipe this module's scratch DB before every test."""
    _redis().flushdb()
    yield


def _make_world():
    tag = "rd" + uuid.uuid4().hex[:8]
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
    user_model = get_user_model()
    owner = user_model.objects.create_user(username=f"owner-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=owner, org=org, role="owner", is_active=True)
    other_org = Organization.objects.create(name=f"other-{tag}")
    stranger = user_model.objects.create_user(username=f"other-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=stranger, org=other_org, role="owner", is_active=True)

    topic = f"{org.org_id}/farms/1/gateways/GW-{tag}/telemetry"
    return {
        "tag": tag, "org": org, "saloon": saloon, "slot": slot,
        "topic": topic, "owner": owner, "stranger": stranger,
    }


def _ingest(world, device_ts="2026-08-25T12:00:00Z", buffered=False):
    payload = json.dumps({
        "gateway_hardware_id": f"hw-{world['tag']}",
        "gateway_slot_id": f"GW-{world['tag']}",
        "schema_version": "2.0",
        "device_ts": device_ts,
        "is_buffered": buffered,
        "readings": [{"pack_id": "P-A", "metrics": {"temp": {"avg": 28.4}}}],
    }).encode()
    assert handle_message(
        _redis(), world["topic"], payload, db_fetch=lambda *a: world["slot"].slot_id
    ) is None
    # push BOTH lanes through the real consumer — the API reads the DB
    drain_stream("telemetry:realtime", count=10)
    drain_stream("telemetry:historical", count=10)


def _login(username: str) -> str:
    client = APIClient()
    response = client.post(
        reverse("accounts:auth-login"),
        {"username": username, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.data
    return response.data["access_token"]


def _as_app_role():
    """SET ROLE morghdari_app — production identity for the REQUEST phase.

    Dev/test connects as morghdari_dev (superuser ⇒ BYPASSRLS), which would
    make every RLS assertion vacuous. Callers MUST reset in finally.
    فارسی: هویتِ واقعیِ اپ برای فاز درخواست؛ در پایان حتماً ریست می‌شود.
    """
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")


def _reset_role():
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_read_shows_own_row_with_contract_fields(timescale_db):
    world = _make_world()
    client = APIClient()
    token = _login(world["owner"].username)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    _ingest(world)
    _as_app_role()  # request phase runs as the production DB identity
    try:
        response = client.get(reverse("telemetry:saloon-telemetry", args=[world["saloon"].id]))
        assert response.status_code == 200
        assert set(response.data) == {"saloon_id", "results"}
        assert len(response.data["results"]) == 1  # buffered EXCLUDED by default
        reading = response.data["results"][0]
        assert set(reading) == {
            "server_received_ts", "device_ts", "is_buffered",
            "buffered_delay_mins", "slot_id", "metrics",
        }
        assert reading["metrics"]["temp"]["avg"] == 28.4
        assert reading["buffered_delay_mins"] is not None  # generated column READ
    finally:
        _reset_role()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_include_buffered_returns_both(timescale_db):
    world = _make_world()
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_login(world['owner'].username)}")
    _ingest(world, "2026-08-25T12:00:00Z", buffered=False)
    _ingest(world, "2026-08-25T11:00:00Z", buffered=True)
    _as_app_role()
    try:
        response = client.get(reverse("telemetry:saloon-telemetry",
                                      args=[world["saloon"].id]) + "?include_buffered=true")
        assert len(response.data["results"]) == 2
    finally:
        _reset_role()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_cross_org_token_sees_empty_200(timescale_db):
    world = _make_world()
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_login(world['stranger'].username)}")
    _ingest(world)
    _as_app_role()  # WITHOUT this, superuser bypass makes the assertion vacuous
    try:
        response = client.get(reverse("telemetry:saloon-telemetry", args=[world["saloon"].id]))
        assert response.status_code == 200
        assert response.data["results"] == []  # fail-closed, not an error (C-14 family)
    finally:
        _reset_role()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_anonymous_gets_401(timescale_db):
    world = _make_world()
    response = APIClient().get(reverse("telemetry:saloon-telemetry", args=[world["saloon"].id]))
    assert response.status_code == 401


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_slot_filter_narrows(timescale_db):
    world = _make_world()
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_login(world['owner'].username)}")
    _ingest(world)
    _as_app_role()
    try:
        response = client.get(
            reverse("telemetry:saloon-telemetry", args=[world["saloon"].id]) + "?slot_id=nope"
        )
        assert response.status_code == 200
        assert response.data["results"] == []
    finally:
        _reset_role()

