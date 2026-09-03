"""/demo/api/farm/ — query-budget regression tests.

The boss board polls this endpoint every 5 seconds, so per-poll SQL and Redis
round-trips are a budget, not a detail. These tests lock in the batched-lookup
contract (django-patterns pass, 2026-09-01):

  * an absolute ceiling on SQL statements per poll;
  * a flat-scaling check — adding saloons must not add per-saloon queries
    (this is the N+1 that used to cost ~+2 statements per saloon); and
  * a Redis round-trip shape check — two mget pipelines per poll, zero
    per-key get/exists calls.

Tests are documentation: they encode HOW MUCH one poll is allowed to cost.
فارسی: هزینهٔ هر پولِ ۵ ثانیه‌ای بودجه است، نه جزئیات — این تست‌ها سقفش را
قفل می‌کنند.
"""

import os
import uuid

import pytest
import redis as redis_lib

# GATE: /demo/api/* exists only when DEBUG or this env var is set.
os.environ.setdefault("MORGHDARI_DEMO_UI", "1")

from tests.conftest import redis_base_url  # noqa: E402

REDIS_URL = redis_base_url() + "/11"  # scratch DB (same isolation as test_demo_farm)

from datetime import datetime, timedelta, timezone as dt_tz  # noqa: E402

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
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
from apps.telemetry.models import FlockCycle  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402

PASSWORD = "strong-pass-123"

# Measured 2026-09-01 against the 2-saloon test world: 13 statements per
# poll (3-farm dev universe also measured 13 — the cost is flat; tenant
# setup + the batched lookups dominate, not org size). Ceiling = measured +
# one regression element: any per-saloon/per-slot loop is +1..2 statements
# per element and blows this immediately. The flat-scaling test below is the
# sharper guard.
SQL_BUDGET_PER_POLL = 17


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    """Wipe the isolated logical DB and bind consumers to it."""
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    yield


class _CountingRedisClient:
    """Counting proxy over a real redis client: records how many times the
    view touches the network per key-accessor (mget/get/exists)."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "calls", {"mget": 0, "get": 0, "exists": 0})

    def __getattr__(self, name):
        if name in self.calls:
            self.calls[name] += 1
        return getattr(self._inner, name)


def _make_world(saloon_count=0, slots_per_saloon=2):
    """Base world: org + farm + owner. Add saloons via _add_saloons so a
    single org can be grown between polls (the flat-scaling test)."""
    tag = "qb" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    user = get_user_model().objects.create_user(username=f"qb-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=user, org=org, role="owner", is_active=True)
    world = {"org": org, "farm": farm, "user": user, "saloons": [], "_n": 0}
    _add_saloons(world, saloon_count, slots_per_saloon=slots_per_saloon)
    return world


def _add_saloons(world, count, slots_per_saloon=2):
    """Create more saloons (with slots, mappings, open flocks) under the
    SAME org — the topology the endpoint must serve cheaply as it grows."""
    tag = f"qb{world['org'].org_id}"[:20]
    for _ in range(count):
        i = world["_n"]
        world["_n"] += 1
        saloon = Saloon.objects.create(
            org=world["org"], farm=world["farm"], name=f"s-{tag}-{i}"
        )
        FlockCycle.objects.create(
            org=world["org"], saloon=saloon, breed_type="ross",
            start_date=datetime.now(tz=dt_tz.utc) - timedelta(days=10),
            placement_count=10000, day_old_weight_g=720,
        )
        for j in range(slots_per_saloon):
            position = SaloonPosition.objects.create(
                saloon=saloon, org=world["org"], label=f"pos-{tag}-{i}-{j}"
            )
            slot = SaloonSensorSlot.objects.create(
                slot_id=f"slot-{tag}-{i}-{j}",
                saloon=saloon, org=world["org"], position=position,
            )
            GatewaySensorMapping.objects.create(
                gateway_hardware_id=f"hw-{tag}-{i}-{j}", pack_id=f"P-{j}",
                slot=slot, org=world["org"],
            )
        world["saloons"].append(saloon)


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
def test_farm_topology_sql_stays_within_budget(timescale_db):
    """One poll may not exceed SQL_BUDGET_PER_POLL statements. A per-saloon
    or per-slot query regression (~+1..2 statements per element) blows this."""
    world = _make_world(saloon_count=2, slots_per_saloon=2)
    token = _login(world["user"].username)
    client = _authed_client(token)

    with CaptureQueriesContext(connection) as ctx:
        response = client.get(reverse("demo-farm"))
    assert response.status_code == 200
    assert len(ctx) <= SQL_BUDGET_PER_POLL, (
        f"/demo/api/farm/ cost {len(ctx)} SQL statements "
        f"(budget {SQL_BUDGET_PER_POLL}) — a per-saloon/per-slot loop has "
        f"regressed.\n{ctx.captured_queries}"
    )


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_topology_queries_do_not_scale_with_saloon_count(timescale_db):
    """The flat line: growing the org 2 → 4 saloons must not add a single
    statement. With batched lookups the cost is independent of org size; the
    old per-saloon loop added ~2 statements per saloon, and this test exists
    to catch it coming back."""
    world = _make_world(saloon_count=2, slots_per_saloon=2)
    token = _login(world["user"].username)
    client = _authed_client(token)

    with CaptureQueriesContext(connection) as ctx_small:
        assert client.get(reverse("demo-farm")).status_code == 200

    _add_saloons(world, 2, slots_per_saloon=2)  # same org doubles in size

    with CaptureQueriesContext(connection) as ctx_grown:
        assert client.get(reverse("demo-farm")).status_code == 200

    assert len(ctx_grown) <= len(ctx_small), (
        f"poll cost grew with org size: {len(ctx_small)} → {len(ctx_grown)} "
        f"statements for 2 → 4 saloons — a per-saloon query loop has "
        f"regressed.\nsmall={ctx_small.captured_queries}\ngrown={ctx_grown.captured_queries}"
    )


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_topology_redis_round_trips_are_pipelined(timescale_db, monkeypatch):
    """Liveness + breach state must arrive in exactly TWO round-trips (the
    status mget and the breach-key mget). Any per-slot exists or per-key get
    regression is visible here as count drift."""
    inner = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    counting = _CountingRedisClient(inner)
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", counting)

    world = _make_world(saloon_count=2, slots_per_saloon=2)
    token = _login(world["user"].username)
    response = _authed_client(token).get(reverse("demo-farm"))

    assert response.status_code == 200
    assert counting.calls["mget"] == 2, counting.calls
    assert counting.calls["exists"] == 0, counting.calls
    assert counting.calls["get"] == 0, counting.calls