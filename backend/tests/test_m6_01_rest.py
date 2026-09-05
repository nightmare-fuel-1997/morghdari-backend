"""M6-01 acceptance — the v1.1 operability REST surface (contract v1.5.0).

Routes under test (16 over 15 endpoints, matching test_openapi_contract.py's
shrunk KNOWN_GAPS):
  saloon detail · saloon alerts · analytics/hourly · farms overview ·
  location-tree · provinces · cities · villages · positions · alert ack ·
  operational-state · flocks GET/POST · metric-types · health · hardware/time

Conventions followed: org comes from the VALIDATED org_id claim and every
queryset carries it (defense-in-depth, sqlite loops identical to Postgres+RLS);
role gates read the frozen roles claim; cross-org single-resource reads 404,
cross-org list reads are empty 200 (C-14 family).

فارسی: پذیرش M6-01 — سطح عملیاتی REST؛ دامنهٔ سازمان از ادعای توکن خوانده
می‌شود و هر پرس‌وجو هم آن را دارد.
"""

import uuid
from datetime import datetime

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.alerts.models import (
    AlertAcknowledgement,
    AlertInstance,
    SaloonOperationalState,
)
from apps.devices.models import SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province, Village
from apps.tenants.models import Organization

PASSWORD = "strong-pass-123"


class _NoopTask:
    def delay(self, *args, **kwargs):
        return None


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
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")

    other_org = Organization.objects.create(name=f"other-{tag}")
    other_farm = Farm.objects.create(
        org=other_org, name=f"of-{tag}", province=province, city=city
    )
    other_saloon = Saloon.objects.create(
        org=other_org, farm=other_farm, name=f"os-{tag}"
    )

    user_model = get_user_model()
    owner = user_model.objects.create_user(username=f"owner-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=owner, org=org, role="owner", is_active=True)
    engineer = user_model.objects.create_user(username=f"eng-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=engineer, org=org, role="engineer", is_active=True)
    worker = user_model.objects.create_user(username=f"worker-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=worker, org=org, role="worker", is_active=True)
    stranger = user_model.objects.create_user(username=f"stranger-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=stranger, org=other_org, role="owner", is_active=True)

    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north-wall")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position, is_active=True
    )
    return {
        "tag": tag, "org": org, "farm": farm, "saloon": saloon, "slot": slot,
        "position": position, "province": province, "city": city, "village": village,
        "other_org": other_org, "other_farm": other_farm, "other_saloon": other_saloon,
        "owner": owner, "engineer": engineer, "worker": worker, "stranger": stranger,
    }


def _make_alert(world, *, status="ACTIVE_TIER_1", source="REALTIME", resolved=False):
    return AlertInstance.objects.create(
        org=world["org"], farm=world["farm"], saloon=world["saloon"],
        metric_type="nh3", current_status=status,
        current_value=42.0, source=source,
        resolved_at=datetime.now() if resolved else None,
        highest_tier_reached=int(status.rsplit("_", 1)[1]) if status.startswith("ACTIVE_TIER_") else 1,
    )


@pytest.mark.django_db
def test_saloon_detail_shape_and_org_scope():
    world = _make_world()
    client = _login(world["owner"])
    response = client.get(reverse("farms:saloon-detail", args=[world["saloon"].pk]))
    assert response.status_code == 200
    body = response.data
    assert body["saloon_id"] == str(world["saloon"].pk)
    assert body["name"] == world["saloon"].name
    assert body["farm_id"] == world["farm"].pk
    assert body["farm_name"] == world["farm"].name
    assert body["org_id"] == str(world["org"].org_id)
    assert body["locations"]["province_id"] == world["farm"].province_id
    assert body["locations"]["city_id"] == world["farm"].city_id
    assert body["locations"]["village_id"] == world["farm"].village_id
    assert body["operational_state"] == "EMPTY_SANITATION"  # never transitioned
    assert body["flock_age_days"] is None
    assert body["gateway_slot_id"] is None
    assert body["gateway_status"] is None
    assert len(body["sensor_slots"]) == 1
    slot = body["sensor_slots"][0]
    assert slot["slot_id"] == world["slot"].slot_id
    assert slot["position_id"] == world["position"].pk
    assert slot["position_label"] == "north-wall"
    assert slot["is_active"] is True

    # Cross-org caller must NOT see the resource (C-14 family).
    foreign = _login(world["stranger"])
    response = foreign.get(reverse("farms:saloon-detail", args=[world["saloon"].pk]))
    assert response.status_code == 404
    assert response.data == {"error": "saloon not found"}


@pytest.mark.django_db
def test_farm_overview_cards_with_alert_counts():
    world = _make_world()
    _make_alert(world, status="ACTIVE_TIER_1")
    _make_alert(world, status="ACTIVE_TIER_3")
    _make_alert(world, status="ACTIVE_TIER_2", resolved=True)

    client = _login(world["owner"])
    response = client.get(reverse("farms:farm-overview", args=[world["farm"].pk]))
    assert response.status_code == 200
    body = response.data
    assert body["farm_id"] == world["farm"].pk
    assert body["name"] == world["farm"].name
    assert len(body["saloons"]) == 1
    card = body["saloons"][0]
    assert card["saloon_id"] == str(world["saloon"].pk)
    assert card["active_alert_count"] == 2  # resolved excluded
    assert card["highest_active_tier"] == 3

    foreign = _login(world["stranger"])
    response = foreign.get(reverse("farms:farm-overview", args=[world["farm"].pk]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_saloon_alerts_filters_and_cross_org_empty():
    world = _make_world()
    active = _make_alert(world, status="ACTIVE_TIER_1")
    replay = _make_alert(world, status="ACTIVE_TIER_2", source="REPLAY_ANALYSIS")
    _make_alert(world, status="ACTIVE_TIER_1", resolved=True)

    client = _login(world["owner"])
    url = reverse("alerts:saloon-alerts", args=[world["saloon"].pk])
    response = client.get(url)
    assert response.status_code == 200
    keys = {row["alert_id"] for row in response.data["results"]}
    assert keys == {str(active.id), str(replay.id)}  # default ACTIVE

    response = client.get(url, {"status": "ALL"})
    assert len(response.data["results"]) == 3

    response = client.get(url, {"status": "ALL", "source": "REPLAY_ANALYSIS"})
    assert [r["alert_id"] for r in response.data["results"]] == [str(replay.id)]

    response = client.get(url, {"status": "BOGUS"})
    assert response.status_code == 400

    foreign = _login(world["stranger"])
    response = foreign.get(url)
    assert response.status_code == 200
    assert response.data["results"] == []  # cross-org list = empty 200


@pytest.mark.django_db
def test_metric_types_catalog():
    world = _make_world()
    client = _login(world["owner"])
    from apps.telemetry.models import SensorMetricType

    SensorMetricType.objects.create(
        metric_key=f"m{world['tag']}", display_name="Custom Metric", unit="ppm", is_tier1=True
    )
    response = client.get(reverse("telemetry:metric-types"))
    assert response.status_code == 200
    row = next(r for r in response.data if r["metric_key"] == f"m{world['tag']}")
    assert row == {
        "metric_key": f"m{world['tag']}",
        "display_name": "Custom Metric",
        "unit": "ppm",
        "value_type": "numeric",
        "is_tier1": True,
        "icon_name": None,
    }


@pytest.mark.django_db
def test_geography_lists_and_param_validation():
    world = _make_world()
    client = _login(world["worker"])

    response = client.get(reverse("provisioning:provisioning-provinces"))
    assert response.status_code == 200
    province_ids = [p["id"] for p in response.data]
    assert world["farm"].province_id in province_ids

    response = client.get(reverse("provisioning:provisioning-cities"), {"province_id": world["farm"].province_id})
    assert response.status_code == 200
    assert [c["id"] for c in response.data] == [world["farm"].city_id]

    response = client.get(reverse("provisioning:provisioning-villages"), {"city_id": world["city"].pk})
    assert response.status_code == 200
    assert [v["id"] for v in response.data] == [world["village"].pk]

    assert client.get(reverse("provisioning:provisioning-cities")).status_code == 400
    assert client.get(reverse("provisioning:provisioning-villages")).status_code == 400


@pytest.mark.django_db
def test_location_tree_scoped_and_positions():
    world = _make_world()
    client = _login(world["owner"])
    url = reverse("provisioning:provisioning-location-tree")
    response = client.get(url)
    assert response.status_code == 200
    own_city = next(
        c
        for province in response.data
        for c in province["cities"]
        if c["id"] == world["farm"].city_id
    )
    own_farms = [f["farm_id"] for f in own_city["farms"]]
    assert world["farm"].pk in own_farms
    assert world["other_farm"].pk not in own_farms  # foreign org never leaks

    # second call served (shape identical) — cached 1h
    assert client.get(url).data == response.data

    # positions: own org sees them, stranger does not (empty list)
    positions_url = reverse(
        "provisioning:provisioning-saloon-positions", args=[world["saloon"].pk]
    )
    response = client.get(positions_url)
    assert response.status_code == 200
    assert [p["id"] for p in response.data] == [world["position"].pk]
    assert response.data[0]["label"] == "north-wall"

    foreign = _login(world["stranger"])
    response = foreign.get(positions_url)
    assert response.status_code == 200
    assert response.data == []


@pytest.mark.django_db
def test_operational_state_lifecycle_and_gates():
    world = _make_world()
    engineer = _login(world["engineer"])
    url = reverse("farms:saloon-operational-state", args=[world["saloon"].pk])

    # untouched saloon reads EMPTY_SANITATION; chain EMPTY → PRE → ACTIVE → HARVESTED → EMPTY
    current = engineer.get(reverse("farms:saloon-detail", args=[world["saloon"].pk])).data
    assert current["operational_state"] == "EMPTY_SANITATION"

    # skipping PRE_HEATING (EMPTY → ACTIVE) → 400 — tested BEFORE any transition
    assert engineer.post(url, {"state": "ACTIVE_FLOCK"}, format="json").status_code == 400
    # bogus state → 400
    assert engineer.post(url, {"state": "FLYING"}, format="json").status_code == 400

    response = engineer.post(url, {"state": "PRE_HEATING", "reason": "cleanup done"}, format="json")
    assert response.status_code == 200
    assert response.data["operational_state"] == "PRE_HEATING"
    # same-state again → 400
    assert engineer.post(url, {"state": "PRE_HEATING"}, format="json").status_code == 400

    assert engineer.post(url, {"state": "ACTIVE_FLOCK"}, format="json").status_code == 200
    assert engineer.post(url, {"state": "HARVESTED_VACANT"}, format="json").status_code == 200
    assert engineer.post(url, {"state": "EMPTY_SANITATION"}, format="json").status_code == 200

    # history written with only the latest row open
    rows = SaloonOperationalState.objects.filter(saloon=world["saloon"]).order_by("entered_at", "id")
    assert list(rows.values_list("state", flat=True)) == [
        "PRE_HEATING", "ACTIVE_FLOCK", "HARVESTED_VACANT", "EMPTY_SANITATION",
    ]
    assert rows.exclude(exited_at__isnull=True).count() == 3

    # role gate: worker cannot write
    worker = _login(world["worker"])
    response = worker.post(url, {"state": "PRE_HEATING"}, format="json")
    assert response.status_code == 403

    # cross-org saloon → 404
    stranger = _login(world["stranger"])
    response = stranger.post(url, {"state": "PRE_HEATING"}, format="json")
    assert response.status_code == 404


@pytest.mark.django_db
def test_flock_cycles_create_and_list(monkeypatch):
    # The FlockCycle post_save signal (alerts) fires cache_saloon_thresholds
    # — eager in tests, needs Redis; stub it so the REST test stays DB-only.
    monkeypatch.setattr("apps.alerts.signals.cache_saloon_thresholds", _NoopTask())

    world = _make_world()
    engineer = _login(world["engineer"])
    url = reverse("farms:flock-cycles", args=[world["saloon"].pk])

    response = engineer.post(
        url,
        {
            "breed_type": "broiler",
            "start_date": "2026-09-01",
            "placement_count": 1000,
            "day_old_weight_g": 42.5,
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    created = response.data
    assert created["breed_type"] == "broiler"
    assert created["end_date"] is None
    assert created["placement_count"] == 1000

    # second flock while one is active → 400 (contract v1.4.2)
    response = engineer.post(
        url, {"breed_type": "layer", "start_date": "2026-09-02", "placement_count": 500},
        format="json",
    )
    assert response.status_code == 400
    assert "active flock" in response.data["error"]

    # placement_count below 1 → 400
    from apps.telemetry.models import FlockCycle

    FlockCycle.objects.filter(saloon=world["saloon"]).update(end_date=datetime.now())
    response = engineer.post(
        url, {"breed_type": "layer", "start_date": "2026-09-03", "placement_count": 0},
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"] == "placement_count must be ≥ 1"

    response = engineer.post(url, {"start_date": "2026-09-03", "placement_count": 5}, format="json")
    assert response.status_code == 400  # breed_type missing
    response = engineer.post(url, {"breed_type": "layer", "placement_count": 5}, format="json")
    assert response.status_code == 400  # start_date missing

    # valid second flock now allowed
    response = engineer.post(
        url, {"breed_type": "layer", "start_date": "2026-09-03", "placement_count": 500},
        format="json",
    )
    assert response.status_code == 201

    # GET lists newest first for any org member; dates echo the Tehran day
    response = _login(world["worker"]).get(url)
    assert response.status_code == 200
    assert [f["breed_type"] for f in response.data] == ["layer", "broiler"]
    assert [f["start_date"] for f in response.data] == ["2026-09-03", "2026-09-01"]
    assert response.data[0]["end_date"] is None  # active flock
    assert response.data[1]["end_date"] is not None  # closed earlier

    # worker cannot POST
    response = _login(world["worker"]).post(
        url, {"breed_type": "layer", "start_date": "2026-09-04", "placement_count": 5},
        format="json",
    )
    assert response.status_code == 403

    # cross-org saloon → 404 on write
    stranger = _login(world["stranger"])
    response = stranger.post(
        url, {"breed_type": "layer", "start_date": "2026-09-04", "placement_count": 5},
        format="json",
    )
    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.use_migrations
def test_alert_acknowledge_endpoint(timescale_db, monkeypatch, fake_redis):
    # The ack SERVICE wraps its writes in tenant_context (set_config GUC) —
    # a Postgres-only facility; on sqlite there is nothing to exercise.
    if connection.vendor != "postgresql":
        pytest.skip("ack service uses the set_config RLS GUC (Postgres only)")
    monkeypatch.setattr("apps.alerts.services.sweeps.system_redis", lambda: fake_redis)
    world = _make_world()
    alert = _make_alert(world, status="ACTIVE_TIER_1")

    url = reverse("alerts:alert-acknowledge", args=[str(alert.id)])
    client = _login(world["worker"])
    response = client.post(url, {"notes": "checked the line"}, format="json")
    assert response.status_code == 200
    assert response.data["alert_id"] == str(alert.id)
    assert response.data["pause_minutes"] == 10

    row = AlertAcknowledgement.objects.get(alert=alert)
    assert row.acknowledged_by_id == world["worker"].pk
    assert row.org_id == world["org"].org_id
    assert row.notes == "checked the line"

    # a bare reader role cannot ack — 403
    from apps.accounts.models import OrgMember

    user_model = get_user_model()
    viewer = user_model.objects.create_user(username=f"viewer-{world['tag']}", password=PASSWORD)
    OrgMember.objects.create(user=viewer, org=world["org"], role="doctor", is_active=True)
    viewer_client = _login(viewer)
    assert viewer_client.post(url, {}, format="json").status_code == 403

    # cross-org: nothing to acknowledge → 404
    stranger = _login(world["stranger"])
    assert stranger.post(url, {}, format="json").status_code == 404

    # unknown alert id → 404
    assert client.post(reverse("alerts:alert-acknowledge", args=[str(uuid.uuid4())]), {}, format="json").status_code == 404


@pytest.mark.django_db
def test_hardware_time_is_public():
    client = APIClient()
    response = client.get(reverse("monitoring:hardware-time"))
    assert response.status_code == 200
    assert abs(response.data["unix_ts"] - int(datetime.now().timestamp())) < 60


@pytest.mark.django_db
def test_health_probe_states(monkeypatch):
    from apps import monitoring

    monkeypatch.setattr(monitoring.views, "_db_probe", lambda: {"status": "ok", "latency_ms": 1.2})
    monkeypatch.setattr(
        monitoring.views,
        "_redis_probe",
        lambda: {
            "status": "ok",
            "ping_ms": 0.4,
            "queue_depth": {"realtime": 0, "historical": 0, "pending_approval": None},
        },
    )
    client = APIClient()
    response = client.get(reverse("monitoring:health"))
    assert response.status_code == 200
    assert response.data["status"] == "healthy"
    assert response.data["services"]["database"]["status"] == "ok"
    assert response.data["services"]["redis"]["status"] == "ok"

    monkeypatch.setattr(monitoring.views, "_redis_probe", lambda: {"status": "error"})
    response = client.get(reverse("monitoring:health"))
    assert response.status_code == 503
    assert response.data["status"] == "degraded"


@pytest.mark.django_db
@pytest.mark.use_migrations
def test_saloon_analytics_hourly(timescale_db):
    """Hourly analytics needs the metric_hourly_aggregates hypertable (Postgres).

    فارسی: نمودار ساعتی جدولِ هایپرتبل می‌خواهد؛ روی sqlite رد می‌شود.
    """
    if connection.vendor != "postgresql":
        pytest.skip("metric_hourly_aggregates is a TimescaleDB hypertable")
    world = _make_world()
    from apps.telemetry.models import SensorMetricType

    SensorMetricType.objects.create(metric_key="temp", display_name="Temperature", unit="C")
    bucket = datetime(2026, 9, 1, 12, 0, 0)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO metric_hourly_aggregates "
            "(bucket_hr, org_id, saloon_id, slot_id, metric_key, avg_val, min_val, max_val, stddev_val, sample_count, high_variance) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [bucket, world["org"].org_id, world["saloon"].pk, world["slot"].slot_id,
             "temp", 28.5, 27.0, 30.0, 0.5, 12, False],
        )
    client = _login(world["owner"])
    url = reverse("telemetry:saloon-analytics-hourly", args=[world["saloon"].pk])
    response = client.get(
        url,
        {
            "from": "2026-09-01T00:00:00Z",
            "to": "2026-09-02T00:00:00Z",
            "metric": "temp",
            "slot_id": world["slot"].slot_id,
        },
    )
    assert response.status_code == 200
    assert response.data["metric"] == "temp"
    assert response.data["slot_id"] == world["slot"].slot_id
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["avg"] == 28.5

    # unknown metric → 400
    response = client.get(
        url,
        {"from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z", "metric": "nope"},
    )
    assert response.status_code == 400
    # missing required params → 400
    assert client.get(url).status_code == 400
