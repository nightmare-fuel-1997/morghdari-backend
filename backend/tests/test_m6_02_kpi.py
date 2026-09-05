"""M6-02 acceptance — the v1.3 KPI + dashboard REST surface (contract v1.5.0).

Routes under test (8 ops, matching test_openapi_contract.py's shrunk
KNOWN_GAPS):
  weight-samples GET/POST · feed-intake GET/POST (per-day upsert) ·
  saloons kpis GET · me/dashboard-config GET/PUT · dashboard-config/{role} GET/PUT

Conventions: org from the VALIDATED org_id claim on every queryset (RLS is the
Postgres backstop); production writes engineer+ for the saloon; role-default
dashboard rows owner-only; cross-org reads 404. D22 NULL rules: FCR null until
weight + feed both exist, compliance null with no bands/no data — never 0,
never an error. The compliance math needs the Timescale hypertable, so that
one test runs Postgres-only (same posture as the M6-01 analytics test).

فارسی: پذیرش M6-02 — نمونهٔ وزن، خوراک روزانه، شاخص‌های D22 و پیکربندی داشبورد.
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
from apps.alerts.models import (
    FlockFeedIntake,
    FlockWeightSample,
    LifecycleTemplate,
    MortalityRecord,
)
from apps.auditing.models import AccountActivityLog
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
    saloon2 = Saloon.objects.create(org=org, farm=farm, name=f"s2-{tag}")

    other_org = Organization.objects.create(name=f"other-{tag}")
    other_farm = Farm.objects.create(
        org=other_org, name=f"of-{tag}", province=province, city=city
    )
    other_saloon = Saloon.objects.create(org=other_org, farm=other_farm, name=f"os-{tag}")

    user_model = get_user_model()
    owner = user_model.objects.create_user(username=f"owner-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=owner, org=org, role="owner", is_active=True)
    engineer = user_model.objects.create_user(username=f"eng-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=engineer, org=org, role="engineer", is_active=True)
    manager = user_model.objects.create_user(username=f"mgr-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=manager, org=org, role="manager", is_active=True)
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
        "tag": tag, "org": org, "farm": farm, "saloon": saloon, "saloon2": saloon2,
        "other_saloon": other_saloon, "flock": flock,
        "owner": owner, "engineer": engineer, "manager": manager,
        "worker": worker, "stranger": stranger,
    }


def _ws_url(world):
    return reverse(
        "alerts:weight-samples", args=[world["saloon"].pk, world["flock"].flock_id]
    )


def _fi_url(world):
    return reverse(
        "alerts:feed-intake", args=[world["saloon"].pk, world["flock"].flock_id]
    )


# ---------------------------------------------------------------------------
# weight-samples
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_weight_samples_post_then_get():
    world = _make_world()
    client = _login(world["engineer"])
    response = client.post(
        _ws_url(world),
        {
            "sampled_at": "2026-09-05T10:00:00+03:30",
            "sample_size": 50,
            "avg_weight_g": 1980.5,
            "min_weight_g": 1720,
            "max_weight_g": 2210,
            "uniformity_pct": 87.5,
            "notes": "pen 3",
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    body = response.data
    assert body["sample_size"] == 50
    assert body["avg_weight_g"] == 1980.5
    assert body["uniformity_pct"] == 87.5
    assert body["source"] == "manual"

    listing = _login(world["owner"]).get(_ws_url(world))
    assert listing.status_code == 200
    assert len(listing.data) == 1
    assert listing.data[0]["avg_weight_g"] == 1980.5
    assert listing.data[0]["sample_size"] == 50


@pytest.mark.django_db
def test_weight_samples_role_gate_and_cross_org_404():
    world = _make_world()
    payload = {"sample_size": 10, "avg_weight_g": 1500}
    assert _login(world["worker"]).post(_ws_url(world), payload, format="json").status_code == 403
    # reads are org-member
    assert _login(world["worker"]).get(_ws_url(world)).status_code == 200
    # stranger (other org) → 404 on both
    stranger = _login(world["stranger"])
    assert stranger.get(_ws_url(world)).status_code == 404
    assert stranger.post(_ws_url(world), payload, format="json").status_code == 404


@pytest.mark.django_db
def test_weight_samples_validation():
    world = _make_world()
    client = _login(world["engineer"])
    assert client.post(_ws_url(world), {"sample_size": 10}, format="json").status_code == 400
    assert client.post(_ws_url(world), {"avg_weight_g": 1}, format="json").status_code == 400
    assert (
        client.post(
            _ws_url(world), {"sample_size": 0, "avg_weight_g": 1}, format="json"
        ).status_code
        == 400
    )
    # unknown flock → 404
    url = reverse("alerts:weight-samples", args=[world["saloon"].pk, 999999])
    assert client.get(url).status_code == 404


# ---------------------------------------------------------------------------
# feed-intake — per-day upsert + audited corrections
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_feed_intake_upsert_and_correction_is_audited():
    world = _make_world()
    client = _login(world["engineer"])
    first = client.post(
        _fi_url(world),
        {"entry_date": "2026-09-03", "feed_kg": 1500, "feed_type": "starter"},
        format="json",
    )
    assert first.status_code == 200, first.data
    assert first.data["corrected"] is False

    second = client.post(
        _fi_url(world), {"entry_date": "2026-09-03", "feed_kg": 1652}, format="json"
    )
    assert second.status_code == 200
    assert second.data["corrected"] is True
    assert second.data["feed_kg"] == 1652

    listing = _login(world["owner"]).get(_fi_url(world))
    assert listing.status_code == 200
    assert len(listing.data) == 1  # ONE row per flock per day
    assert listing.data[0]["feed_kg"] == 1652

    audit = AccountActivityLog.objects.filter(action="feed_intake_correction")
    assert audit.count() == 1
    details = audit.first().details
    assert details["old_value"] == 1500
    assert details["new_value"] == 1652
    assert details["flock_id"] == world["flock"].flock_id


@pytest.mark.django_db
def test_feed_intake_validation_and_gates():
    world = _make_world()
    client = _login(world["engineer"])
    assert (
        client.post(_fi_url(world), {"feed_kg": 100}, format="json").status_code == 400
    )
    assert (
        client.post(
            _fi_url(world),
            {"entry_date": "2026-09-03", "feed_kg": 100, "feed_type": "mystery"},
            format="json",
        ).status_code
        == 400
    )
    worker = _login(world["worker"])
    assert (
        worker.post(
            _fi_url(world), {"entry_date": "2026-09-03", "feed_kg": 100}, format="json"
        ).status_code
        == 403
    )
    assert worker.get(_fi_url(world)).status_code == 200
    # other org → 404
    assert _login(world["stranger"]).get(_fi_url(world)).status_code == 404


# ---------------------------------------------------------------------------
# saloons/{id}/kpis/ — D22 math + NULL rules
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_kpis_null_rules_and_empty_flock():
    world = _make_world()
    client = _login(world["owner"])
    response = client.get(reverse("alerts:saloon-kpis", args=[world["saloon"].pk]))
    assert response.status_code == 200
    body = response.data
    assert body["flock_id"] == world["flock"].flock_id
    # D22 gives cumulative mortality NO NULL clause (only placement_count
    # guards it) — zero deaths is a true 0.0, like the shipped quality.py.
    assert body["mortality_pct"] == 0.0
    assert body["daily_mortality_pct"] == []
    assert body["compliance_pct"] is None  # no bands → nothing to comply with
    assert body["fcr"] is None  # D22: NULL until weight + feed both exist — never 0
    summary = body["cycle_summary"]
    assert summary["days_elapsed"] == 9  # closed flock: end − start, deterministic
    assert summary["cumulative_mortality_pct"] == 0.0
    assert summary["latest_fcr"] is None
    assert summary["alert_count"] == 0

    # metrics subset + unknown key → 400
    subset = client.get(
        reverse("alerts:saloon-kpis", args=[world["saloon"].pk]),
        {"metrics": "fcr"},
    )
    assert subset.status_code == 200
    assert set(subset.data) == {"flock_id", "fcr"}
    bad = client.get(
        reverse("alerts:saloon-kpis", args=[world["saloon"].pk]),
        {"metrics": "fcr,nope"},
    )
    assert bad.status_code == 400


@pytest.mark.django_db
def test_kpis_mortality_and_fcr_math():
    world = _make_world()
    flock = world["flock"]
    MortalityRecord.objects.create(
        org=world["org"], flock=flock, saloon=world["saloon"],
        recorded_by=world["engineer"], count=10,
        occurred_at=datetime(2026, 9, 2, 9, 0, tzinfo=TEHRAN),
    )
    MortalityRecord.objects.create(
        org=world["org"], flock=flock, saloon=world["saloon"],
        recorded_by=world["engineer"], count=5,
        occurred_at=datetime(2026, 9, 4, 9, 0, tzinfo=TEHRAN),
    )
    FlockWeightSample.objects.create(
        org=world["org"], flock=flock, saloon=world["saloon"],
        sampled_at=datetime(2026, 9, 5, 10, 0, tzinfo=TEHRAN),
        sample_size=50, avg_weight_g=2000, recorded_by=world["engineer"],
    )
    FlockFeedIntake.objects.create(
        org=world["org"], flock=flock, saloon=world["saloon"],
        entry_date=datetime(2026, 9, 2).date(), feed_kg=1500,
        recorded_by=world["engineer"],
    )
    FlockFeedIntake.objects.create(
        org=world["org"], flock=flock, saloon=world["saloon"],
        entry_date=datetime(2026, 9, 3).date(), feed_kg=1652,
        recorded_by=world["engineer"],
    )

    body = _login(world["owner"]).get(
        reverse("alerts:saloon-kpis", args=[world["saloon"].pk])
    ).data
    assert body["mortality_pct"] == 1.5  # 15 ÷ 1000
    daily = {row["date"]: row["pct"] for row in body["daily_mortality_pct"]}
    assert daily["2026-09-02"] == 1.01  # 10 ÷ 990
    assert daily["2026-09-04"] == 0.51  # 5 ÷ 985
    # FCR = 3152 kg ÷ ((2000−45) g × 985 birds ÷ 1000) = 1.637
    assert body["fcr"] == 1.637
    assert body["cycle_summary"]["cumulative_mortality_pct"] == 1.5
    assert body["cycle_summary"]["latest_fcr"] == 1.637


@pytest.mark.django_db
def test_kpis_flock_scope_and_404s():
    world = _make_world()
    client = _login(world["owner"])
    # saloon without any flock → 404
    assert client.get(reverse("alerts:saloon-kpis", args=[world["saloon2"].pk])).status_code == 404
    # explicit flock_id of ANOTHER saloon (same org) → 404, not the other flock's data
    response = client.get(
        reverse("alerts:saloon-kpis", args=[world["saloon2"].pk]),
        {"flock_id": world["flock"].flock_id},
    )
    assert response.status_code == 404
    # other org's saloon → 404
    assert client.get(reverse("alerts:saloon-kpis", args=[world["other_saloon"].pk])).status_code == 404
    # explicit flock_id of the saloon's own flock works
    ok = client.get(
        reverse("alerts:saloon-kpis", args=[world["saloon"].pk]),
        {"flock_id": world["flock"].flock_id, "metrics": "mortality_pct"},
    )
    assert ok.status_code == 200
    assert ok.data["flock_id"] == world["flock"].flock_id


# ---------------------------------------------------------------------------
# dashboard-config — resolution personal → role default → system default
# ---------------------------------------------------------------------------

def _me_url():
    return reverse("alerts:me-dashboard-config")


def _role_url(role):
    return reverse("alerts:role-dashboard-config", args=[role])


@pytest.mark.django_db
def test_dashboard_config_resolution_chain():
    world = _make_world()
    owner = _login(world["owner"])
    manager = _login(world["manager"])

    # 1) no rows at all → every widget from the built-in system default
    mine = manager.get(_me_url()).data
    assert mine["role"] == "manager"
    assert mine["widgets"]["fcr"] is True
    assert set(mine["resolved_from"].values()) == {"system_default"}

    # 2) owner sets the MANAGER role default: fcr off
    saved = owner.put(_role_url("manager"), {"widgets": {"fcr": False}}, format="json")
    assert saved.status_code == 200
    assert saved.data["is_system_default"] is False

    mine = manager.get(_me_url()).data
    assert mine["widgets"]["fcr"] is False
    assert mine["resolved_from"]["fcr"] == "role_default"
    assert mine["resolved_from"]["alerts"] == "system_default"

    # 3) manager's personal override: alerts off
    mine = manager.put(_me_url(), {"widgets": {"alerts": False}}, format="json").data
    assert mine["resolved_from"]["alerts"] == "personal"
    assert mine["resolved_from"]["fcr"] == "role_default"

    # 4) partial personal update keeps the other personal key (merge, not replace)
    mine = manager.put(_me_url(), {"widgets": {"fcr": True}}, format="json").data
    assert mine["widgets"]["fcr"] is True and mine["resolved_from"]["fcr"] == "personal"
    assert mine["widgets"]["alerts"] is False and mine["resolved_from"]["alerts"] == "personal"

    # 5) role GET (no row) → system default; engineer's role default still system
    role_get = owner.get(_role_url("engineer")).data
    assert role_get["is_system_default"] is True
    assert role_get["widgets"] == _system_widgets()


def _system_widgets():
    from apps.alerts.kpi_views import SYSTEM_WIDGETS

    return dict(SYSTEM_WIDGETS)


@pytest.mark.django_db
def test_dashboard_config_role_gate_and_validation():
    world = _make_world()
    owner = _login(world["owner"])
    manager = _login(world["manager"])

    # role defaults are an owner surface
    assert (
        manager.put(_role_url("engineer"), {"widgets": {"fcr": False}}, format="json").status_code
        == 403
    )
    assert owner.put(_role_url("engineer"), {"widgets": {"fcr": False}}, format="json").status_code == 200

    # GET role row is org-member readable
    assert manager.get(_role_url("engineer")).status_code == 200

    # unknown role / invalid payloads
    assert owner.get(_role_url("vice_emperor")).status_code == 400
    assert owner.put(_role_url("worker"), {"widgets": {"nope": True}}, format="json").status_code == 400
    assert owner.put(_role_url("worker"), {"widgets": {"fcr": "yes"}}, format="json").status_code == 400
    assert owner.put(_me_url(), {"widgets": "fcr"}, format="json").status_code == 400

    # cross-org isolation: stranger's role rows are their own
    stranger = _login(world["stranger"])
    assert stranger.get(_role_url("owner")).status_code == 200
    body = stranger.get(_role_url("owner")).data
    assert body["is_system_default"] is True  # owner's writes never leaked


# ---------------------------------------------------------------------------
# compliance % — needs the Timescale hypertable (Postgres-only, like M6-01)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.use_migrations
def test_compliance_pct_banded_hours(timescale_db):
    if connection.vendor != "postgresql":
        pytest.skip("metric_hourly_aggregates is a TimescaleDB hypertable")
    world = _make_world()
    flock = world["flock"]
    template = LifecycleTemplate.objects.create(
        org=world["org"], breed_type="ross308", is_default=True
    )
    from apps.alerts.models import LifecycleThresholdRule

    LifecycleThresholdRule.objects.create(
        template=template, day_from=0, day_to=45,
        metric="temp", min_value=18, max_value=34,
    )

    # Two hourly buckets on flock day 1: one in band (30°C), one out (40°C).
    # Raw INSERT — high_variance is a DB-generated column (see M6-01 test).
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

    client = _login(world["owner"])
    response = client.get(
        reverse("alerts:saloon-kpis", args=[world["saloon"].pk]),
        {"metrics": "compliance_pct", "from": "2026-09-01", "to": "2026-09-01"},
    )
    assert response.status_code == 200
    assert response.data["compliance_pct"] == 50.0  # 1 compliant ÷ 2 with data

    # cycle_summary carries the same compliance number
    summary = client.get(
        reverse("alerts:saloon-kpis", args=[world["saloon"].pk]),
        {"metrics": "cycle_summary", "from": "2026-09-01", "to": "2026-09-01"},
    ).data["cycle_summary"]
    assert summary["compliance_pct"] == 50.0

    # window outside any data → hours-with-data is 0 → NULL, never 0
    empty = client.get(
        reverse("alerts:saloon-kpis", args=[world["saloon"].pk]),
        {"metrics": "compliance_pct", "from": "2026-09-09", "to": "2026-09-09"},
    )
    assert empty.status_code == 200
    assert empty.data["compliance_pct"] is None
