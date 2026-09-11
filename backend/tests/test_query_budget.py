"""/api/v1/farms/{id}/overview/ — query-budget regression tests.

The native farm overview (contract v1.6, M7-11) is polled by the frontend, so
per-poll SQL is a budget, not a detail. These tests lock in the batched-lookup
contract (django-patterns pass, 2026-09-01; retargeted from the deleted demo
board endpoint, 2026-09-11):

  * an absolute ceiling on SQL statements per poll;
  * a flat-scaling check — adding saloons must not add per-saloon queries
    (this is the N+1 that used to cost ~+2 statements per saloon): the farm
    overview used to call the per-saloon operational-state lookup inside its
    card loop, which this guard caught when it moved here.

Tests are documentation: they encode HOW MUCH one poll is allowed to cost.
فارسی: هزینهٔ هر پول بودجه است، نه جزئیات — این تست‌ها سقفش را قفل می‌کنند.
"""

import uuid

import pytest

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

# Measured 2026-09-11 (Postgres/Timescale CI, 2-saloon : 2-slot world): 10
# statements per poll — farm, saloons, open flocks, gateway zones, alert
# aggregates, operational states and locations, all bulk. Ceiling = measured
# + 1, so ANY added per-poll statement is visible; a per-saloon loop is +1
# per element and cannot hide. The flat-scaling test below is the second lock.
SQL_BUDGET_PER_POLL = 11


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


def _overview_url(world):
    return reverse("farms:farm-overview", args=[world["farm"].pk])


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_overview_sql_stays_within_budget(timescale_db):
    """One poll may not exceed SQL_BUDGET_PER_POLL statements. A per-saloon
    or per-slot query regression (~+1..2 statements per element) blows this."""
    world = _make_world(saloon_count=2, slots_per_saloon=2)
    token = _login(world["user"].username)
    client = _authed_client(token)

    with CaptureQueriesContext(connection) as ctx:
        response = client.get(_overview_url(world))
    assert response.status_code == 200
    assert len(ctx) <= SQL_BUDGET_PER_POLL, (
        f"/api/v1/farms/<id>/overview/ cost {len(ctx)} SQL statements "
        f"(budget {SQL_BUDGET_PER_POLL}) — a per-saloon/per-slot loop has "
        f"regressed.\n{ctx.captured_queries}"
    )


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_overview_queries_do_not_scale_with_saloon_count(timescale_db):
    """The flat line: growing the farm 2 → 4 saloons must not add a single
    statement. With batched lookups the cost is independent of farm size; the
    old per-saloon loop added ~2 statements per saloon, and this test exists
    to catch it coming back."""
    world = _make_world(saloon_count=2, slots_per_saloon=2)
    token = _login(world["user"].username)
    client = _authed_client(token)

    with CaptureQueriesContext(connection) as ctx_small:
        assert client.get(_overview_url(world)).status_code == 200

    _add_saloons(world, 2, slots_per_saloon=2)  # same org doubles in size

    with CaptureQueriesContext(connection) as ctx_grown:
        assert client.get(_overview_url(world)).status_code == 200

    assert len(ctx_grown) <= len(ctx_small), (
        f"poll cost grew with farm size: {len(ctx_small)} → {len(ctx_grown)} "
        f"statements for 2 → 4 saloons — a per-saloon query loop has "
        f"regressed.\nsmall={ctx_small.captured_queries}\ngrown={ctx_grown.captured_queries}"
    )
