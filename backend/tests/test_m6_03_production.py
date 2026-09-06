"""M6-03 acceptance — the v1.4 water/biosecurity/capabilities surface.

Routes under test (5 ops over 3 endpoints — the KNOWN_GAPS set they empty):
  water-intake GET/POST (per-day upsert, audited corrections — feed twin) ·
  biosecurity-events GET/POST (any org member records; enum + filters) ·
  saloons/{id}/capabilities GET (D23 computed truth)

Conventions (M6-01/02): org from the VALIDATED org_id claim on every
queryset; production writes engineer+; cross-org ids → 404; D23 spirit —
a missing input renders a feature UNAVAILABLE with missing_inputs +
enable_path, never 0, never an error. The capability rules are pure
(feature_matrix unit tests run without a DB); enrichment tests (sensor
slots ∪ seen-telemetry keys, log row-existence) run on any backend, and the
unavailable→available demo runs Postgres-only because it inserts into the
unmanaged saloon_telemetry hypertable (same posture as the M6-02
compliance test).

فارسی: پذیرش M6-03 — آب روزانه، دفتر زیست‌امنیتی و نمای توانمندی D23.
"""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.alerts.models import (
    FlockFeedIntake,
    FlockWaterIntake,
    FlockWeightSample,
    LifecycleTemplate,
    LifecycleThresholdRule,
)
from apps.alerts.services.capability_profile import (
    FEATURE_MATRIX,
    compute_profile,
    evaluate_feature,
)
from apps.auditing.models import AccountActivityLog
from apps.devices.models import SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry.models import FlockCycle
from apps.tenants.models import Organization

TEHRAN = ZoneInfo("Asia/Tehran")
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


def _make_world(*, with_flock=True):
    tag = uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")

    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")

    other_org = Organization.objects.create(name=f"other-{tag}")
    other_farm = Farm.objects.create(
        org=other_org, name=f"of-{tag}", province=province, city=city
    )

    user_model = get_user_model()
    owner = user_model.objects.create_user(username=f"owner-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=owner, org=org, role="owner", is_active=True)
    engineer = user_model.objects.create_user(username=f"eng-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=engineer, org=org, role="engineer", is_active=True)
    worker = user_model.objects.create_user(username=f"worker-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=worker, org=org, role="worker", is_active=True)
    stranger = user_model.objects.create_user(username=f"str-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=stranger, org=other_org, role="owner", is_active=True)

    flock = None
    if with_flock:
        flock = FlockCycle.objects.create(
            org=org,
            saloon=saloon,
            breed_type="ross308",
            start_date=datetime(2026, 9, 1, 8, 0, tzinfo=TEHRAN),
            end_date=datetime(2026, 9, 10, 8, 0, tzinfo=TEHRAN),
            placement_count=1000,
            day_old_weight_g=45,
        )
    return {
        "tag": tag, "org": org, "farm": farm, "other_farm": other_farm,
        "saloon": saloon, "flock": flock,
        "owner": owner, "engineer": engineer, "worker": worker, "stranger": stranger,
    }


def _wi_url(world):
    return reverse(
        "alerts:water-intake", args=[world["saloon"].pk, world["flock"].flock_id]
    )


def _bio_url(farm):
    return reverse("alerts:biosecurity-events", args=[farm.pk])


def _cap_url(world):
    return reverse("alerts:saloon-capabilities", args=[world["saloon"].pk])


# ---------------------------------------------------------------------------
# water-intake — per-day upsert + audited corrections (the feed twin)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_water_intake_upsert_and_correction_is_audited():
    world = _make_world()
    client = _login(world["engineer"])
    first = client.post(
        _wi_url(world), {"entry_date": "2026-09-03", "water_l": 3200}, format="json"
    )
    assert first.status_code == 200, first.data
    assert first.data["corrected"] is False
    assert first.data["water_l"] == 3200
    assert first.data["source"] == "manual"

    second = client.post(
        _wi_url(world), {"entry_date": "2026-09-03", "water_l": 3410}, format="json"
    )
    assert second.status_code == 200
    assert second.data["corrected"] is True
    assert second.data["water_l"] == 3410

    listing = _login(world["owner"]).get(_wi_url(world))
    assert listing.status_code == 200
    assert len(listing.data) == 1  # ONE row per flock per day
    assert listing.data[0]["water_l"] == 3410
    assert listing.data[0]["entry_date"] == "2026-09-03"

    audit = AccountActivityLog.objects.filter(action="water_intake_correction")
    assert audit.count() == 1
    details = audit.first().details
    assert details["old_value"] == 3200
    assert details["new_value"] == 3410
    assert details["flock_id"] == world["flock"].flock_id


@pytest.mark.django_db
def test_water_intake_validation_and_gates():
    world = _make_world()
    client = _login(world["engineer"])
    # missing / invalid payloads → 400
    assert client.post(_wi_url(world), {"water_l": 100}, format="json").status_code == 400
    assert (
        client.post(
            _wi_url(world), {"entry_date": "2026-09-03", "water_l": "lots"}, format="json"
        ).status_code
        == 400
    )
    # worker writes are gated, reads are org-member
    worker = _login(world["worker"])
    assert (
        worker.post(
            _wi_url(world), {"entry_date": "2026-09-03", "water_l": 100}, format="json"
        ).status_code
        == 403
    )
    assert worker.get(_wi_url(world)).status_code == 200
    # other org → 404 (both verbs)
    stranger = _login(world["stranger"])
    assert stranger.get(_wi_url(world)).status_code == 404
    assert (
        stranger.post(
            _wi_url(world), {"entry_date": "2026-09-03", "water_l": 1}, format="json"
        ).status_code
        == 404
    )
    # unknown flock → 404
    url = reverse("alerts:water-intake", args=[world["saloon"].pk, 999999])
    assert client.get(url).status_code == 404


@pytest.mark.django_db
def test_water_intake_get_date_window():
    world = _make_world()
    flock = world["flock"]
    engineer = world["engineer"]
    for day, liters in ((2, 3100), (3, 3200), (4, 3350)):
        FlockWaterIntake.objects.create(
            org=world["org"], flock=flock, saloon=world["saloon"],
            entry_date=datetime(2026, 9, day).date(), water_l=liters,
            recorded_by=engineer,
        )
    listing = _login(world["owner"]).get(
        _wi_url(world), {"from": "2026-09-03", "to": "2026-09-04"}
    )
    assert listing.status_code == 200
    assert [row["entry_date"] for row in listing.data] == ["2026-09-03", "2026-09-04"]


# ---------------------------------------------------------------------------
# biosecurity-events — the avian-flu evidence log
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_biosecurity_post_then_get_roundtrip():
    world = _make_world()
    # ANY org member records (worker included) — yaml: "any staff role may record"
    response = _login(world["worker"]).post(
        _bio_url(world["farm"]),
        {
            "event_type": "visitor_entry",
            "occurred_at": "2026-09-04T09:30:00+03:30",
            "person_count": 3,
            "details": {"origin": "Hatchery Co", "purpose": "delivery"},
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    body = response.data
    assert body["event_type"] == "visitor_entry"
    assert body["person_count"] == 3

    listing = _login(world["owner"]).get(_bio_url(world["farm"]))
    assert listing.status_code == 200
    assert len(listing.data) == 1  # newest first
    assert listing.data[0]["event_type"] == "visitor_entry"
    assert listing.data[0]["recorded_by"] == world["worker"].pk

    # vet/insurance query: filter by type + window
    filtered = _login(world["owner"]).get(
        _bio_url(world["farm"]),
        {"event_type": "disinfection", "from": "2026-09-01T00:00:00+03:30"},
    )
    assert filtered.status_code == 200
    assert filtered.data == []


@pytest.mark.django_db
def test_biosecurity_validation_and_gates():
    world = _make_world()
    client = _login(world["engineer"])
    # enum enforced (the DB check would reject it too — this is the friendly 400)
    assert (
        client.post(
            _bio_url(world["farm"]), {"event_type": "alien_visit"}, format="json"
        ).status_code
        == 400
    )
    assert client.post(_bio_url(world["farm"]), {}, format="json").status_code == 400
    assert (
        client.post(
            _bio_url(world["farm"]),
            {"event_type": "other", "person_count": "many"},
            format="json",
        ).status_code
        == 400
    )
    assert (
        client.post(
            _bio_url(world["farm"]),
            {"event_type": "other", "details": "not-an-object"},
            format="json",
        ).status_code
        == 400
    )
    # from/to are GET filters, not body fields — POST ignores them
    assert (
        client.get(_bio_url(world["farm"]), {"from": "not-a-date"}).status_code == 400
    )
    # other org's farm → 404
    stranger = _login(world["stranger"])
    assert stranger.get(_bio_url(world["farm"])).status_code == 404
    assert (
        stranger.post(
            _bio_url(world["farm"]), {"event_type": "other"}, format="json"
        ).status_code
        == 404
    )
    # stranger's own farm works
    assert (
        stranger.post(
            _bio_url(world["other_farm"]), {"event_type": "other"}, format="json"
        ).status_code
        == 201
    )


@pytest.mark.django_db
def test_biosecurity_event_types_cover_contract_enum():
    """Every yaml enum value passes validation (guards contract/code drift)."""
    from apps.alerts.production_views import BIOSECURITY_EVENT_TYPES

    yaml_enum = {
        "visitor_entry", "vehicle_entry", "disinfection",
        "rodent_control", "outbreak_prep", "other",
    }
    assert BIOSECURITY_EVENT_TYPES == yaml_enum


# ---------------------------------------------------------------------------
# capability profile — pure matrix math (no DB)
# ---------------------------------------------------------------------------

def test_capability_matrix_covers_v14_features():
    """M6-03 seeded six; M6-05 completed the v1 D23 set with the D22 KPIs."""
    assert set(FEATURE_MATRIX) == {
        "fcr", "water_ratio", "thi", "co", "power", "biosecurity_log",
        "mortality_pct", "compliance_pct",
    }


def test_evaluate_feature_missing_metrics_and_logs():
    result = evaluate_feature(
        "thi",
        FEATURE_MATRIX["thi"],
        live_metrics={"temp"},
        available_logs={},
    )
    assert result == {
        "feature": "thi",
        "available": False,
        "missing_inputs": ["humidity"],
        "enable_path": "Install temp + humidity sensor packs",
    }


def test_evaluate_feature_power_any_of():
    # EITHER power source satisfies the requirement
    mains = evaluate_feature(
        "power", FEATURE_MATRIX["power"], {"power_mains"}, {}
    )
    gen = evaluate_feature(
        "power", FEATURE_MATRIX["power"], {"power_gen"}, {}
    )
    none = evaluate_feature("power", FEATURE_MATRIX["power"], set(), {})
    assert mains["available"] and gen["available"]
    assert not none["available"]
    assert none["missing_inputs"] == ["power_gen", "power_mains"]


def test_evaluate_feature_logs_use_human_labels():
    result = evaluate_feature(
        "fcr", FEATURE_MATRIX["fcr"], set(), {"weight_samples": True}
    )
    assert result["missing_inputs"] == ["feed logs"]
    assert result["enable_path"] == "Log a weight sample or connect a weigh station"


def test_evaluate_feature_biosecurity_needs_nothing():
    result = evaluate_feature(
        "biosecurity_log", FEATURE_MATRIX["biosecurity_log"], set(), {}
    )
    assert result["available"] is True
    assert result["missing_inputs"] == []
    assert result["enable_path"] == ""


def test_compute_profile_shape_and_live_metrics_sorted():
    profile = compute_profile(
        7,
        {"humidity", "temp", "co"},
        {"weight_samples": True, "feed_intake": True, "water_intake": True},
    )
    assert profile["saloon_id"] == 7
    assert profile["live_metrics"] == ["co", "humidity", "temp"]
    by_name = {f["feature"]: f for f in profile["features"]}
    assert by_name["thi"]["available"] is True
    assert by_name["fcr"]["available"] is True
    assert by_name["water_ratio"]["available"] is True
    assert by_name["co"]["available"] is True
    assert by_name["power"]["available"] is False
    assert by_name["biosecurity_log"]["available"] is True


# ---------------------------------------------------------------------------
# capabilities endpoint — enrichment + role/read gates
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.use_migrations
def test_capabilities_logs_only_world(timescale_db):
    """No sensors at all: sensor features unavailable, log features follow rows.
    (use_migrations + timescale_db so the saloon_telemetry hypertable exists
    on Postgres even in a solo-module run; SQLite skips the DDL.)"""
    world = _make_world()
    FlockWeightSample.objects.create(
        org=world["org"], flock=world["flock"], saloon=world["saloon"],
        sampled_at=datetime(2026, 9, 5, 10, 0, tzinfo=TEHRAN),
        sample_size=50, avg_weight_g=2000, recorded_by=world["engineer"],
    )
    FlockFeedIntake.objects.create(
        org=world["org"], flock=world["flock"], saloon=world["saloon"],
        entry_date=datetime(2026, 9, 5).date(), feed_kg=1500,
        recorded_by=world["engineer"],
    )

    body = _login(world["owner"]).get(_cap_url(world)).data
    assert body["saloon_id"] == world["saloon"].pk
    assert body["live_metrics"] == []  # no slots, no telemetry
    by_name = {f["feature"]: f for f in body["features"]}
    assert by_name["fcr"]["available"] is True  # both logs exist
    assert by_name["water_ratio"]["available"] is False
    assert by_name["water_ratio"]["missing_inputs"] == ["water logs"]
    assert by_name["thi"]["available"] is False
    assert by_name["thi"]["missing_inputs"] == ["humidity", "temp"]
    assert by_name["thi"]["enable_path"] == "Install temp + humidity sensor packs"
    # biosecurity is a pure manual log — always available
    assert by_name["biosecurity_log"]["available"] is True
    # M6-05: the D22 KPI features follow the flock's row/computed facts
    assert by_name["mortality_pct"]["available"] is False  # flock exists but no mortality rows
    assert by_name["mortality_pct"]["missing_inputs"] == ["mortality records"]
    assert by_name["compliance_pct"]["available"] is False  # no bands, no hourly data


@pytest.mark.django_db
@pytest.mark.use_migrations
def test_capabilities_reads_are_org_member_and_cross_org_404(timescale_db):
    world = _make_world()
    assert _login(world["worker"]).get(_cap_url(world)).status_code == 200
    assert _login(world["stranger"]).get(_cap_url(world)).status_code == 404


# ---------------------------------------------------------------------------
# unavailable → available demo (TUTORIAL subtask 7) — needs the hypertable
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.use_migrations
def test_capabilities_unavailable_to_available_around_telemetry(timescale_db):
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("saloon_telemetry is a TimescaleDB hypertable")
    world = _make_world()
    position = SaloonPosition.objects.create(
        org=world["org"], saloon=world["saloon"], label="north-wall"
    )
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-{world['tag']}", org=world["org"],
        saloon=world["saloon"], position=position,
        allowed_metric_keys=["temp"],
    )
    # M6-05 demo: threshold bands exist for the flock-age window, so compliance
    # lacks ONLY its hourly data before the reading arrives.
    template = LifecycleTemplate.objects.create(
        org=world["org"], breed_type="ross308", is_default=True
    )
    LifecycleThresholdRule.objects.create(
        template=template, day_from=0, day_to=45,
        metric="temp", min_value=18, max_value=34,
    )

    # Allowed keys alone make THI unavailable (humidity missing)…
    body = _login(world["owner"]).get(_cap_url(world)).data
    thi = next(f for f in body["features"] if f["feature"] == "thi")
    assert thi["available"] is False
    assert body["live_metrics"] == ["temp"]
    compliance = next(f for f in body["features"] if f["feature"] == "compliance_pct")
    assert compliance["available"] is False
    assert compliance["missing_inputs"] == ["hourly sensor data"]

    # …a reading carrying the seen key flips the profile — computed truth.
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO saloon_telemetry "
            "(device_ts, org_id, saloon_id, slot_id, metrics) "
            "VALUES (%s, %s, %s, %s, %s)",
            [
                datetime(2026, 9, 5, 9, 0, tzinfo=TEHRAN),
                world["org"].org_id, world["saloon"].pk, slot.slot_id,
                '{"temp": {"avg": 30.1}, "humidity": {"avg": 55.0}}',
            ],
        )
    body = _login(world["owner"]).get(_cap_url(world)).data
    assert "humidity" in body["live_metrics"] and "temp" in body["live_metrics"]
    thi = next(f for f in body["features"] if f["feature"] == "thi")
    assert thi["available"] is True
    assert thi["missing_inputs"] == []
    # M6-05 acceptance: bands + data now BOTH exist → compliance AVAILABLE.
    # (The hourly_data fact tracks metric_hourly_aggregates — the SAME input
    # kpi_service.compliance_pct computes from — not raw readings, which only
    # feed the rollup. Insert one aggregated bucket and the feature flips.)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO metric_hourly_aggregates "
            "(bucket_hr, org_id, saloon_id, slot_id, metric_key, avg_val, min_val, max_val, stddev_val, sample_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                datetime(2026, 9, 1, 9, 0, tzinfo=TEHRAN),
                world["org"].org_id, world["saloon"].pk, slot.slot_id,
                "temp", 30.1, 29.0, 31.0, 0.5, 12,
            ],
        )
    body = _login(world["owner"]).get(_cap_url(world)).data
    compliance = next(f for f in body["features"] if f["feature"] == "compliance_pct")
    assert compliance["available"] is True
    assert compliance["missing_inputs"] == []
    assert compliance["enable_path"] == ""
