"""M7-12 acceptance — farm telemetry merge + newest-first paging (v1.6 native).

Routes under test (yaml +1 path + 2 documented params):
  GET /api/v1/farms/{farm_id}/telemetry/ — the farm's saloon streams merged
  into ONE TelemetryReading list (rows gain saloon_id); the farm list equals
  the UNION of the per-saloon lists for the same window.
  GET /api/v1/saloons/{saloon_id}/telemetry/?limit=/?offset= — paging mode is
  NEWEST-FIRST (?limit=1 IS the latest reading; the Flutter chart calls
  ?limit=24); no-param behavior stays chronological ascending (byte-compat).

Conventions: the claim org rides every queryset explicitly (the M6-01
defense-in-depth — now on the telemetry reads too), cross-org LIST reads are
empty 200 (C-14 family), single-resource farm reads are 404.

فارسی: پذیرش M7-12 — ادغام تله‌متری مزرعه و صفحه‌بندی از نو به قدیم؛
?limit=1 یعنی آخرین خواندن.
"""

import uuid
from datetime import datetime, timezone as dt_timezone

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.devices.models import SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry.models import SaloonTelemetry
from apps.tenants.models import Organization

PASSWORD = "strong-pass-123"


@pytest.fixture(autouse=True)
def _telemetry_stack():
    """sqlite quick loop: the hypertable migration is PG-guarded, so replay a
    portable saloon_telemetry DDL carrying the exact columns the ORM touches —
    INCLUDING buffered_delay_mins as a STORED generated column (sqlite's own
    expression of the same ts delta), so the view's RawSQL annotation runs the
    identical code path on both vendors. test.py's "create from state" intent,
    same shape as the M6-09 compat fixture. On PG the timescale_db argument
    does the real work (cascade flush + hypertable replay)."""
    if connection.vendor != "sqlite":
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS saloon_telemetry ("
            "server_received_ts TIMESTAMP NOT NULL PRIMARY KEY, device_ts TIMESTAMP NOT NULL, "
            "org_id VARCHAR(32) NOT NULL, saloon_id INTEGER NOT NULL, slot_id VARCHAR(50) NOT NULL, "
            "is_buffered BOOLEAN, clock_drift BOOLEAN, metrics TEXT NOT NULL, "
            "buffered_delay_mins NUMERIC GENERATED ALWAYS AS ("
            "(julianday(server_received_ts) - julianday(device_ts)) * 1440.0) STORED)"
        )
        # Per-test isolation: sqlite teardown never truncates unmanaged tables,
        # and server_received_ts is the PK — stale rows would collide.
        cursor.execute("DELETE FROM saloon_telemetry")
    yield


def _ts(minute: int) -> datetime:
    return datetime(2026, 9, 7, 10, minute, tzinfo=dt_timezone.utc)


def _login(role_user):
    client = APIClient()
    response = client.post(
        reverse("accounts:auth-login"),
        {"username": role_user.username, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.data
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}")
    return client


def _make_world():
    tag = "ft" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")

    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    other_farm = Farm.objects.create(org=org, name=f"f2-{tag}", province=province, city=city)
    saloon_a = Saloon.objects.create(org=org, farm=farm, name=f"sa-{tag}")
    saloon_b = Saloon.objects.create(org=org, farm=farm, name=f"sb-{tag}")

    position = SaloonPosition.objects.create(saloon=saloon_a, org=org, label="north")
    slot_a = SaloonSensorSlot.objects.create(
        slot_id=f"slot-a-{tag}", saloon=saloon_a, org=org, position=position
    )
    slot_b = SaloonSensorSlot.objects.create(
        slot_id=f"slot-b-{tag}", saloon=saloon_b, org=org, position=position
    )

    other_org = Organization.objects.create(name=f"other-{tag}")
    foreign_farm = Farm.objects.create(
        org=other_org, name=f"ff-{tag}", province=province, city=city
    )

    user_model = get_user_model()
    owner = user_model.objects.create_user(username=f"owner-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=owner, org=org, role="owner", is_active=True)
    stranger = user_model.objects.create_user(username=f"stranger-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=stranger, org=other_org, role="owner", is_active=True)

    return {
        "tag": tag, "org": org, "farm": farm, "other_farm": other_farm,
        "saloon_a": saloon_a, "saloon_b": saloon_b,
        "slot_a": slot_a, "slot_b": slot_b,
        "foreign_farm": foreign_farm, "owner": owner, "stranger": stranger,
    }


def _seed(org, saloon, slot, minute, *, buffered=False, metrics=None):
    return SaloonTelemetry.objects.create(
        org=org,
        saloon=saloon,
        slot=slot,
        device_ts=_ts(minute),
        server_received_ts=_ts(minute),
        metrics=metrics or {"temp": {"avg": 20.0 + minute, "min": 20.0, "max": 21.0}},
        is_buffered=buffered,
    )


def _stamp(minute: int) -> str:
    """DRF renders aware datetimes with the +00:00 suffix."""
    return _ts(minute).isoformat()


def _key(row):
    """Reading identity across BOTH read shapes — per-saloon rows carry no
    saloon_id (the path implies it), farm rows do."""
    return (row["device_ts"], row["slot_id"])


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_telemetry_equals_union_of_saloon_streams(timescale_db):
    world = _make_world()
    _seed(world["org"], world["saloon_a"], world["slot_a"], 1)
    _seed(world["org"], world["saloon_a"], world["slot_a"], 3)
    _seed(world["org"], world["saloon_b"], world["slot_b"], 2)
    _seed(world["org"], world["saloon_b"], world["slot_b"], 4)
    _seed(world["org"], world["saloon_a"], world["slot_a"], 5, buffered=True)

    client = _login(world["owner"])
    response = client.get(reverse("telemetry:farm-telemetry", args=[world["farm"].pk]))
    assert response.status_code == 200
    body = response.data
    assert body["farm_id"] == world["farm"].pk

    # Default excludes buffered (v1.4.4 semantics) — 4 live rows merged.
    results = body["results"]
    assert len(results) == 4
    # Chronological ascending without paging, each row carrying its saloon.
    assert [_key(row)[0] for row in results] == sorted(_key(row)[0] for row in results)
    assert {row["saloon_id"] for row in results} == {
        str(world["saloon_a"].pk), str(world["saloon_b"].pk)
    }
    assert {row["slot_id"] for row in results} == {world["slot_a"].slot_id, world["slot_b"].slot_id}

    # THE acceptance contract: farm list == union of the per-saloon lists.
    union = []
    for saloon in (world["saloon_a"], world["saloon_b"]):
        per = client.get(reverse("telemetry:saloon-telemetry", args=[saloon.pk]))
        assert per.status_code == 200
        union.extend(_key(row) for row in per.data["results"])
    assert sorted(_key(row) for row in results) == sorted(union)

    # include_buffered adds the buffered row on the farm read too.
    widened = client.get(
        reverse("telemetry:farm-telemetry", args=[world["farm"].pk]),
        {"include_buffered": "true"},
    )
    assert widened.status_code == 200
    assert len(widened.data["results"]) == 5
    assert any(row["is_buffered"] for row in widened.data["results"])


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_telemetry_paging_newest_first_and_offset(timescale_db):
    world = _make_world()
    _seed(world["org"], world["saloon_a"], world["slot_a"], 1)
    _seed(world["org"], world["saloon_b"], world["slot_b"], 2)
    _seed(world["org"], world["saloon_a"], world["slot_a"], 3)
    _seed(world["org"], world["saloon_b"], world["slot_b"], 4)

    client = _login(world["owner"])
    url = reverse("telemetry:farm-telemetry", args=[world["farm"].pk])

    # ?limit=1 IS the latest reading.
    response = client.get(url, {"limit": 1})
    assert response.status_code == 200
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["device_ts"] == _stamp(4)

    # ?limit=24 chart call: every row, newest first.
    chart = client.get(url, {"limit": 24})
    stamps = [row["device_ts"] for row in chart.data["results"]]
    assert stamps == sorted(stamps, reverse=True)
    assert stamps[0] == _stamp(4)

    # offset skips the newest N.
    paged = client.get(url, {"limit": 2, "offset": 1})
    stamps = [row["device_ts"] for row in paged.data["results"]]
    assert stamps == [
        _stamp(3),
        _stamp(2),
    ]


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_saloon_telemetry_paging_validation_and_legacy_default(timescale_db):
    world = _make_world()
    _seed(world["org"], world["saloon_a"], world["slot_a"], 1)
    _seed(world["org"], world["saloon_a"], world["slot_a"], 2)
    _seed(world["org"], world["saloon_a"], world["slot_a"], 3)

    client = _login(world["owner"])
    url = reverse("telemetry:saloon-telemetry", args=[world["saloon_a"].pk])

    # No params: byte-compat legacy behavior — chronological ascending.
    legacy = client.get(url)
    stamps = [row["device_ts"] for row in legacy.data["results"]]
    assert stamps == [_stamp(1),
                      _stamp(2),
                      _stamp(3)]

    # ?limit=1 = the latest reading.
    latest = client.get(url, {"limit": 1})
    assert latest.data["results"][0]["device_ts"] == _stamp(3)

    # offset without limit: full history minus the newest N, newest first.
    paged = client.get(url, {"offset": 1})
    stamps = [row["device_ts"] for row in paged.data["results"]]
    assert stamps == [_stamp(2),
                      _stamp(1)]

    # Validation: a typo is a 400, never a silent empty chart.
    for params, error_fragment in (
        ({"limit": "abc"}, "limit"),
        ({"limit": 0}, "limit"),
        ({"limit": 1001}, "limit"),
        ({"offset": -1}, "offset"),
        ({"offset": "xyz"}, "offset"),
    ):
        response = client.get(url, params)
        assert response.status_code == 400, params
        assert error_fragment in response.data["error"]


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_telemetry_reads_carry_the_claim_org(timescale_db):
    """Defense-in-depth: the explicit org filter makes the C-14 empty-200
    contract true on EVERY backend, not only where RLS is watching."""
    world = _make_world()
    _seed(world["org"], world["saloon_a"], world["slot_a"], 1)

    client = _login(world["owner"])

    foreign = _login(world["stranger"])
    # Cross-org LIST read → empty 200 (C-14 family).
    response = foreign.get(reverse("telemetry:saloon-telemetry", args=[world["saloon_a"].pk]))
    assert response.status_code == 200
    assert response.data["results"] == []

    # Cross-org farm read → 404 (single-resource verdict, same as overview).
    response = foreign.get(reverse("telemetry:farm-telemetry", args=[world["farm"].pk]))
    assert response.status_code == 404
    assert response.data == {"error": "farm not found"}

    # Unknown farm id → the same 404.
    response = client.get(reverse("telemetry:farm-telemetry", args=[999_999]))
    assert response.status_code == 404


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_telemetry_requires_authentication(timescale_db):
    world = _make_world()
    client = APIClient()
    assert client.get(reverse("telemetry:farm-telemetry", args=[world["farm"].pk])).status_code == 401
