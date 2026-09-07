"""M7-11 acceptance — the v1.6 native list endpoints (Tracker §4o gap-fill).

Routes under test (yaml +2 paths):
  GET /api/v1/farms/   → FarmOverview[]   (?org_id= optional, foreign → 403)
  GET /api/v1/saloons/ → SaloonDetail[]   (?farm_id= filter, foreign/unknown → 403)

Conventions: org comes from the VALIDATED org_id claim and every queryset
carries it (defense-in-depth, sqlite loops identical to Postgres+RLS); these
are LIST surfaces so the explicit foreign-org selector is a 403 (you asked for
another org's data), never an empty list — while a nonexistent selector shares
the foreign verdict (no existence oracle).

فارسی: پذیرش M7-11 — فهرست‌های بومی v1.6؛ سازمان از ادعای توکن و درخواستِ
سازمانِ دیگر ۴۰۳ است نه فهرستِ خالی.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province, Village
from apps.tenants.models import Organization

PASSWORD = "strong-pass-123"


def _login(role_user):
    client = APIClient()
    response = client.post(
        reverse("accounts:auth-login"),
        {"username": role_user.username, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.data
    token = response.data["access_token"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _make_world():
    tag = uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    village = Village.objects.create(city=city, name=f"v-{tag}")

    org = Organization.objects.create(name=f"org-{tag}")
    farm_a = Farm.objects.create(org=org, name=f"fa-{tag}", province=province, city=city)
    farm_b = Farm.objects.create(org=org, name=f"fb-{tag}", province=province, city=city)
    saloon_a1 = Saloon.objects.create(org=org, farm=farm_a, name=f"s-a1-{tag}")
    saloon_a2 = Saloon.objects.create(org=org, farm=farm_a, name=f"s-a2-{tag}")
    saloon_b1 = Saloon.objects.create(org=org, farm=farm_b, name=f"s-b1-{tag}")

    other_org = Organization.objects.create(name=f"other-{tag}")
    other_farm = Farm.objects.create(
        org=other_org, name=f"of-{tag}", province=province, city=city
    )
    Saloon.objects.create(org=other_org, farm=other_farm, name=f"os-{tag}")

    user_model = get_user_model()
    owner = user_model.objects.create_user(username=f"owner-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=owner, org=org, role="owner", is_active=True)
    stranger = user_model.objects.create_user(username=f"stranger-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=stranger, org=other_org, role="owner", is_active=True)

    return {
        "tag": tag, "org": org,
        "farm_a": farm_a, "farm_b": farm_b,
        "saloon_a1": saloon_a1, "saloon_a2": saloon_a2, "saloon_b1": saloon_b1,
        "other_org": other_org, "other_farm": other_farm,
        "owner": owner, "stranger": stranger,
    }


@pytest.mark.django_db
def test_farm_list_returns_overviews_for_own_org():
    world = _make_world()
    client = _login(world["owner"])

    response = client.get(reverse("farms:farm-list"))
    assert response.status_code == 200
    farms = response.data
    assert [farm["farm_id"] for farm in farms] == [world["farm_a"].pk, world["farm_b"].pk]

    first = farms[0]
    assert first["name"] == world["farm_a"].name
    assert first["locations"]["province_id"] == world["farm_a"].province_id
    assert first["locations"]["village_id"] == world["farm_a"].village_id
    assert [card["saloon_id"] for card in first["saloons"]] == [
        str(world["saloon_a1"].pk), str(world["saloon_a2"].pk)
    ]
    # Card shape = the farm_overview card (same fields the M6-01 endpoint serves).
    card = first["saloons"][0]
    assert set(card) == {
        "saloon_id", "name", "operational_state", "flock_age_days",
        "gateway_status", "gateway_slot_id", "active_alert_count", "highest_active_tier",
    }
    assert card["operational_state"] == "EMPTY_SANITATION"
    assert card["active_alert_count"] == 0

    # The stranger's list only ever shows the stranger's org.
    foreign = _login(world["stranger"])
    response = foreign.get(reverse("farms:farm-list"))
    assert response.status_code == 200
    assert [farm["farm_id"] for farm in response.data] == [world["other_farm"].pk]


@pytest.mark.django_db
def test_farm_list_org_id_param_mismatch_is_403_not_empty():
    world = _make_world()
    client = _login(world["owner"])

    # Explicit own-org echo is honored.
    response = client.get(reverse("farms:farm-list"), {"org_id": str(world["org"].org_id)})
    assert response.status_code == 200
    assert len(response.data) == 2

    # A foreign org selector is a 403 — you asked for another org's data.
    response = client.get(reverse("farms:farm-list"), {"org_id": str(world["other_org"].org_id)})
    assert response.status_code == 403
    assert response.data == {"error": "forbidden"}


@pytest.mark.django_db
def test_saloon_list_returns_full_details():
    world = _make_world()
    client = _login(world["owner"])

    response = client.get(reverse("farms:saloon-list"))
    assert response.status_code == 200
    saloons = response.data
    # farm + id order: farm_a's two saloons then farm_b's one.
    assert [row["saloon_id"] for row in saloons] == [
        str(world["saloon_a1"].pk), str(world["saloon_a2"].pk), str(world["saloon_b1"].pk)
    ]

    # Shape = the saloon detail body (M6-01), byte for byte.
    row = saloons[0]
    assert set(row) == {
        "saloon_id", "name", "farm_id", "farm_name", "locations", "org_id",
        "operational_state", "flock_age_days", "gateway_slot_id", "gateway_status",
        "sensor_slots",
    }
    assert row["farm_id"] == world["farm_a"].pk
    assert row["org_id"] == str(world["org"].org_id)
    assert row["operational_state"] == "EMPTY_SANITATION"
    assert row["sensor_slots"] == []

    # The stranger never sees the caller's saloons.
    foreign = _login(world["stranger"])
    response = foreign.get(reverse("farms:saloon-list"))
    assert response.status_code == 200
    assert len(response.data) == 1
    assert response.data[0]["farm_id"] == world["other_farm"].pk


@pytest.mark.django_db
def test_saloon_list_farm_filter_foreign_and_unknown_share_403():
    world = _make_world()
    client = _login(world["owner"])

    response = client.get(reverse("farms:saloon-list"), {"farm_id": world["farm_a"].pk})
    assert response.status_code == 200
    assert [row["saloon_id"] for row in response.data] == [
        str(world["saloon_a1"].pk), str(world["saloon_a2"].pk)
    ]

    response = client.get(reverse("farms:saloon-list"), {"farm_id": world["farm_b"].pk})
    assert response.status_code == 200
    assert [row["saloon_id"] for row in response.data] == [str(world["saloon_b1"].pk)]

    # Foreign farm → 403, and an unknown id gets the SAME verdict (no oracle).
    for selector in (world["other_farm"].pk, 999_999):
        response = client.get(reverse("farms:saloon-list"), {"farm_id": selector})
        assert response.status_code == 403
        assert response.data == {"error": "forbidden"}

    # A scalar that is not an integer is a typo, not a filter.
    response = client.get(reverse("farms:saloon-list"), {"farm_id": "abc"})
    assert response.status_code == 400


@pytest.mark.django_db
def test_native_lists_require_authentication():
    _make_world()
    client = APIClient()
    assert client.get(reverse("farms:farm-list")).status_code == 401
    assert client.get(reverse("farms:saloon-list")).status_code == 401
