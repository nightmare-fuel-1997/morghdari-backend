"""M6-04 acceptance — the KPI computation service (D22 math, §2.7/§2.8).

The service (apps/alerts/services/kpi.py) is the ONE home of D22 math; the
/kpis/ view delegates to it. Named suites per Saas.md D22/D23: test_fcr_math,
test_kpi_mortality_pct, test_compliance_pct (endpoint-level in
test_m6_02_kpi.py), test_water_feed_ratio (D23 §2.8 — the water:feed ratio,
industry band ~1.8–2:1, NULL until BOTH water and feed logs exist).

Every function returns (value, reason): the tuple IS D22+D23 as types —
NULL travels WITH its reason, never 0, never an error. The math and
band/rotation helpers are pure (no DB); the compliance aggregation needs the
Timescale hypertable (Postgres-only, same posture as the M6-02 endpoint test).

فارسی: پذیرش M6-04 — سرویس ریاضیِ KPI؛ تاپلِ (مقدار، دلیل) یعنی قانونِ
NULL-با-دلیلِ D22/D23 به شکل نوع، و نسبت آب:خوراک تا تأیید قرارداد فقط سرویس.
"""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule
from apps.alerts.services import kpi as kpi_service
from apps.alerts.services.capability_profile import FEATURE_MATRIX, Requires, evaluate_feature
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry.models import FlockCycle
from apps.tenants.models import Organization

TEHRAN = ZoneInfo("Asia/Tehran")
PASSWORD = "strong-pass-123"


def _make_world():
    tag = uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")

    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")

    user_model = get_user_model()
    owner = user_model.objects.create_user(username=f"owner-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=owner, org=org, role="owner", is_active=True)

    flock = FlockCycle.objects.create(
        org=org,
        saloon=saloon,
        breed_type="ross308",
        start_date=datetime(2026, 9, 1, 8, 0, tzinfo=TEHRAN),
        end_date=datetime(2026, 9, 10, 8, 0, tzinfo=TEHRAN),
        placement_count=1000,
        day_old_weight_g=45,
    )
    return {"tag": tag, "org": org, "farm": farm, "saloon": saloon, "flock": flock, "owner": owner}


# ---------------------------------------------------------------------------
# test_fcr_math — D22 FCR, NULL-until-inputs
# ---------------------------------------------------------------------------

def test_fcr_math():
    fcr = kpi_service.fcr_value
    # D22: NULL until BOTH inputs — never 0, never an error.
    assert fcr(None, 2000, 985, 45) == (None, kpi_service.REASON_FCR_INPUTS)
    assert fcr(3152, None, 985, 45) == (None, kpi_service.REASON_FCR_INPUTS)
    assert fcr(None, None, 985, 45) == (None, kpi_service.REASON_FCR_INPUTS)
    # live count unknown/zero → unavailable (not a division by zero)
    assert fcr(3152, 2000, 0, 45) == (None, kpi_service.REASON_NO_LIVE)
    assert fcr(3152, 2000, None, 45) == (None, kpi_service.REASON_NO_LIVE)
    # non-positive gain → no meaningful FCR
    assert fcr(3152, 40, 985, 45) == (None, kpi_service.REASON_NO_LIVE)
    # NULL day_old_weight_g → treated as 0 (documented ~1.6 % approximation):
    # 1500 kg ÷ ((1000 g − 0) × 1000 birds ÷ 1000) = 1.5
    value, reason = fcr(1500, 1000, 1000, None)
    assert (value, reason) == (1.5, None)
    # canonical worked example: 3152 kg ÷ ((2000−45) g × 985 ÷ 1000)
    assert fcr(3152, 2000, 985, 45) == (1.637, None)


# ---------------------------------------------------------------------------
# test_kpi_mortality_pct — D22 mortality
# ---------------------------------------------------------------------------

def test_kpi_mortality_pct():
    mortality = kpi_service.mortality_pct
    # zero deaths is a TRUE 0.0 (D22 has no NULL clause beyond placement)
    assert mortality(0, 1000) == (0.0, None)
    assert mortality(15, 1000) == (1.5, None)
    assert mortality(1, 3) == (33.33, None)
    # the only NULL guard D22 gives: placement_count
    assert mortality(5, None) == (None, kpi_service.REASON_NO_PLACEMENT)
    assert mortality(5, 0) == (None, kpi_service.REASON_NO_PLACEMENT)


# ---------------------------------------------------------------------------
# test_water_feed_ratio — D23 §2.8
# ---------------------------------------------------------------------------

def test_water_feed_ratio():
    ratio = kpi_service.water_feed_ratio
    # NULL until BOTH logs exist — the D22/D23 degradation rule
    assert ratio(None, 1500) == (None, kpi_service.REASON_NO_WATER)
    assert ratio(3200, None) == (None, kpi_service.REASON_NO_FEED)
    assert ratio(None, None) == (None, kpi_service.REASON_NO_WATER)
    # zero/negative feed cannot produce a ratio
    assert ratio(3200, 0) == (None, kpi_service.REASON_NO_FEED)
    # industry band ~1.8–2:1 (business-numbers-v1): 3200 L ÷ 1600 kg = 2.0
    value, reason = ratio(3200, 1600)
    assert (value, reason) == (2.0, None)
    value, reason = ratio(2900, 1600)
    assert (value, reason) == (1.81, None)
    # rounding to 2 dp keeps the band readable: 2955 ÷ 1637 = 1.8051… → 1.81
    assert ratio(2955, 1637)[0] == 1.81


def test_water_feed_ratio_matrix_and_endpoint_ready():
    """The ratio's requirements are IN the D23 matrix already (water+feed logs);
    the metric value is computed by the service — endpoint exposure waits for
    the M6-10 ratification (yaml frozen until then)."""
    requires = FEATURE_MATRIX["water_ratio"]
    assert requires.logs == ("water_intake", "feed_intake")
    # a farm with both logs sees the feature available; the number comes from
    # kpi_service.water_feed_ratio
    assert evaluate_feature(
        "water_ratio", requires, set(), {"water_intake": True, "feed_intake": True}
    )["available"] is True


def test_declared_requirements_never_derived_in_views():
    """D23: requirements are declared in the matrix — the view never derives
    them. A hypothetical feature asks Requires for its missing inputs."""
    hypothetical = Requires(
        logs=("weight_samples",), enable_path="hypothetical"
    )
    result = evaluate_feature("hypothetical", hypothetical, set(), {"weight_samples": False})
    assert result["missing_inputs"] == ["weight samples"]
    assert result["enable_path"] == "hypothetical"


# ---------------------------------------------------------------------------
# M6-05 — the D22 KPI features completed the v1 D23 matrix
# ---------------------------------------------------------------------------

def test_matrix_kpi_features_requirements():
    """D23 table row: 'FCR / mortality % / compliance % (D22) | placement_count
    + feed/weight logs | v1'. Compliance availability is COMPUTED (bands +
    hours-with-data), declared via Requires.computed."""
    assert FEATURE_MATRIX["mortality_pct"].logs == ("placement", "mortality_records")
    assert FEATURE_MATRIX["compliance_pct"].computed == ("threshold_bands", "hourly_data")


def test_evaluate_mortality_pct_availability():
    requires = FEATURE_MATRIX["mortality_pct"]
    ok = evaluate_feature(
        "mortality_pct", requires, set(),
        {"placement": True, "mortality_records": True},
    )
    assert ok == {
        "feature": "mortality_pct",
        "available": True,
        "missing_inputs": [],
        "enable_path": "",
    }
    partial = evaluate_feature(
        "mortality_pct", requires, set(), {"placement": True}
    )
    assert partial["available"] is False
    assert partial["missing_inputs"] == ["mortality records"]
    assert partial["enable_path"] == (
        "Register the flock's placement count and record daily mortality"
    )


def test_evaluate_compliance_pct_computed_facts():
    requires = FEATURE_MATRIX["compliance_pct"]
    ok = evaluate_feature(
        "compliance_pct", requires, set(),
        {"threshold_bands": True, "hourly_data": True},
    )
    assert ok["available"] is True
    bands_only = evaluate_feature(
        "compliance_pct", requires, set(),
        {"threshold_bands": True, "hourly_data": False},
    )
    assert bands_only["available"] is False
    assert bands_only["missing_inputs"] == ["hourly sensor data"]
    assert bands_only["enable_path"] == (
        "Add flock-age threshold rules and keep sensor packs reporting"
    )


# ---------------------------------------------------------------------------
# test_compliance_pct — band helpers + NULL rules (aggregation is PG-only)
# ---------------------------------------------------------------------------

def test_hour_compliant_banded_metrics_only():
    bands = {"temp": (18, 34)}
    # banded metric inside → compliant
    assert kpi_service.hour_compliant([("temp", 30.0)], bands) is True
    # below/above the band → non-compliant
    assert kpi_service.hour_compliant([("temp", 10.0)], bands) is False
    assert kpi_service.hour_compliant([("temp", 40.0)], bands) is False
    # unbanded metrics are never judged (D23)
    assert kpi_service.hour_compliant([("co", 999.0)], bands) is True
    # band with an open end
    assert kpi_service.hour_compliant([("temp", 40.0)], {"temp": (18, None)}) is True
    # a banded metric with no value that hour is skipped, not judged
    assert kpi_service.hour_compliant([("temp", None)], bands) is True


@pytest.mark.django_db
def test_bands_for_age_days_shape():
    assert kpi_service.bands_for_age_days(5, None) == {}
    world = _make_world()
    template = LifecycleTemplate.objects.create(
        org=world["org"], breed_type="ross308", is_default=True
    )
    LifecycleThresholdRule.objects.create(
        template=template, day_from=0, day_to=45, metric="temp", min_value=18, max_value=34,
    )
    LifecycleThresholdRule.objects.create(
        template=template, day_from=0, day_to=45, metric="humidity", min_value=40, max_value=None,
    )
    bands = kpi_service.bands_for_age_days(5, template.template_id)
    assert bands == {"temp": (18, 34), "humidity": (40, None)}


@pytest.mark.django_db
def test_compliance_null_rules_without_db_windows():
    """No template resolution / empty window → (None, reason), never 0."""
    world = _make_world()
    # flock with no template anywhere → unavailable with reason
    value, reason = kpi_service.compliance_pct(
        world["flock"], [datetime(2026, 9, 2).date()]
    )
    assert value is None
    assert reason in (kpi_service.REASON_NO_BANDS, kpi_service.REASON_NO_DATA)


@pytest.mark.django_db
@pytest.mark.use_migrations
def test_compliance_pct_aggregation(timescale_db):
    """The hours-with-data math on real hourly rows (Postgres/Timescale only)."""
    from rest_framework.test import APIClient

    if connection.vendor != "postgresql":
        pytest.skip("metric_hourly_aggregates is a TimescaleDB hypertable")
    world = _make_world()
    template = LifecycleTemplate.objects.create(
        org=world["org"], breed_type="ross308", is_default=True
    )
    LifecycleThresholdRule.objects.create(
        template=template, day_from=0, day_to=45, metric="temp", min_value=18, max_value=34,
    )
    # One in-band hour, one out-of-band hour on flock day 1.
    for hour, avg in ((10, 30.0), (11, 40.0)):
        bucket = datetime(2026, 9, 1, hour, 0, tzinfo=TEHRAN)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO metric_hourly_aggregates "
                "(bucket_hr, org_id, saloon_id, slot_id, metric_key, avg_val, min_val, max_val, stddev_val, sample_count) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [bucket, world["org"].org_id, world["saloon"].pk, "slot-x",
                 "temp", avg, avg - 1, avg + 1, 0.5, 12],
            )
    value, reason = kpi_service.compliance_pct(
        world["flock"], [datetime(2026, 9, 1).date()]
    )
    assert (value, reason) == (50.0, None)  # 1 compliant ÷ 2 with data


# ---------------------------------------------------------------------------
# The /kpis/ endpoint still speaks the SAME numbers through the service
# ---------------------------------------------------------------------------

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


@pytest.mark.django_db
def test_kpis_endpoint_matches_service_math():
    from apps.alerts.models import FlockFeedIntake, FlockWeightSample, MortalityRecord

    world = _make_world()
    flock = world["flock"]
    owner = world["owner"]
    MortalityRecord.objects.create(
        org=world["org"], flock=flock, saloon=world["saloon"],
        recorded_by=owner, count=10,
        occurred_at=datetime(2026, 9, 2, 9, 0, tzinfo=TEHRAN),
    )
    FlockWeightSample.objects.create(
        org=world["org"], flock=flock, saloon=world["saloon"],
        sampled_at=datetime(2026, 9, 5, 10, 0, tzinfo=TEHRAN),
        sample_size=50, avg_weight_g=2000, recorded_by=owner,
    )
    FlockFeedIntake.objects.create(
        org=world["org"], flock=flock, saloon=world["saloon"],
        entry_date=datetime(2026, 9, 3).date(), feed_kg=3152,
        recorded_by=owner,
    )
    body = _login(owner).get(reverse("alerts:saloon-kpis", args=[world["saloon"].pk])).data
    assert body["mortality_pct"] == 1.0  # 10 deaths ÷ 1000 placed
    assert body["fcr"] == kpi_service.fcr_value(3152, 2000, 990, 45)[0]
    assert body["cycle_summary"]["latest_fcr"] == body["fcr"]
