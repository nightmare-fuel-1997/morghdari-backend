"""M4-11 — MET-ALERT-QUALITY (Observability §5.2) quality metrics.

mttr_seconds_by_tier / ack_to_resolve_seconds / alerts_per_flock + snapshot()
run on REAL rows.

فارسی: پذیرش M4-11 — متریک‌های کیفیت هشدار روی ردیف‌های واقعی می‌خزند.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone as dj_tz

from apps.alerts.models import AlertAcknowledgement, AlertInstance, MortalityRecord
from apps.farms.models import Farm, Saloon
from apps.telemetry.models import FlockCycle
from apps.geography.models import City, Province
from apps.tenants.models import Organization

User = get_user_model()


def _org(tag=None):
    tag = tag or ("q" + uuid.uuid4().hex[:8])
    province = Province.objects.create(name=f"p-{tag}")
    City.objects.create(province=province, name=f"c-{tag}")
    return Organization.objects.create(name=f"org-{tag}")


def _saloon(org, tag=None):
    tag = tag or ("s" + uuid.uuid4().hex[:8])
    province = Province.objects.create(name=f"fp-{tag}")
    city = City.objects.create(province=province, name=f"fc-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    return Saloon.objects.create(org=org, farm=farm, name=f"sal-{tag}")


def _resolved(org, tier, seconds, metric="temp", source="REALTIME", saloon=None):
    if saloon is None:
        saloon = _saloon(org)
    now = dj_tz.now()
    return AlertInstance.objects.create(
        org=org,
        farm=saloon.farm,
        saloon=saloon,
        triggered_at=now - timedelta(seconds=seconds),
        resolved_at=now,
        highest_tier_reached=tier,
        metric_type=metric,
        source=source,
        current_status="RESOLVED",
        current_value="1.0",
    )


@pytest.mark.django_db(transaction=True)
def test_mttr_seconds_by_tier_groups_correctly():
    org = _org()
    _resolved(org, tier=1, seconds=600)
    _resolved(org, tier=1, seconds=1200)
    _resolved(org, tier=2, seconds=300)
    _resolved(org, tier=4, seconds=60)

    from apps.alerts.services.quality import mttr_seconds_by_tier

    out = mttr_seconds_by_tier()
    assert out[1] == pytest.approx(900.0)
    assert out[2] == pytest.approx(300.0)
    assert out[4] == pytest.approx(60.0)


@pytest.mark.django_db(transaction=True)
def test_mttr_seconds_by_tier_empty_db():
    from apps.alerts.services.quality import mttr_seconds_by_tier

    assert mttr_seconds_by_tier() == {}


@pytest.mark.django_db(transaction=True)
def test_mttr_ignores_unresolved_alerts():
    org = _org()
    saloon = _saloon(org)
    _resolved(org, tier=1, seconds=300, saloon=saloon)
    AlertInstance.objects.create(
        org=org,
        farm=saloon.farm,
        saloon=saloon,
        triggered_at=dj_tz.now(), highest_tier_reached=1,
        metric_type="temp", source="REALTIME", current_status="ACTIVE_TIER_1",
        current_value="1.0",
    )  # resolved_at NULL → excluded

    from apps.alerts.services.quality import mttr_seconds_by_tier

    out = mttr_seconds_by_tier()
    assert list(out.keys()) == [1]
    assert out[1] == pytest.approx(300.0)


def _user(org=None, tag=None):
    tag = tag or ("u" + uuid.uuid4().hex[:8])
    return User.objects.create_user(username=tag, password="pass")


# ── ack_to_resolve_seconds ──────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
def test_ack_to_resolve_seconds_split():
    org = _org()
    user = _user(org)
    a = _resolved(org, tier=1, seconds=600)
    b = _resolved(org, tier=1, seconds=2400)   # never acked
    c = _resolved(org, tier=2, seconds=900)

    AlertAcknowledgement.objects.create(
        org=org,
        alert=a,
        acknowledged_by=user,
        acknowledged_at=a.triggered_at + timedelta(seconds=120),
        pause_minutes=60,
    )
    AlertAcknowledgement.objects.create(
        org=org,
        alert=c,
        acknowledged_by=user,
        acknowledged_at=c.triggered_at + timedelta(seconds=240),
        pause_minutes=60,
    )

    from apps.alerts.services.quality import ack_to_resolve_seconds

    out = ack_to_resolve_seconds()
    # acked: a (600s) + c (900s) => mean 750.0; never acked: b (2400s)
    assert out["acked_mean_seconds"] == pytest.approx(750.0)
    assert out["never_acked_mean_seconds"] == pytest.approx(2400.0)


@pytest.mark.django_db(transaction=True)
def test_ack_to_resolve_empty_db_returns_zeros():
    from apps.alerts.services.quality import ack_to_resolve_seconds

    out = ack_to_resolve_seconds()
    assert out["acked_mean_seconds"] == 0.0
    assert out["never_acked_mean_seconds"] == 0.0


# ── alerts_per_flock (mortality % vs placement) ──────────────────────────────


@pytest.mark.django_db(transaction=True)
def test_alerts_per_flock_mortality_ratio_under_threshold():
    org = _org()
    s = _saloon(org)
    now = dj_tz.now()
    user = _user()
    cycle = FlockCycle.objects.create(
        org=org, saloon=s, breed_type="ross308", placement_count=10000,
        start_date=now - timedelta(days=35), end_date=now,
    )
    MortalityRecord.objects.create(
        org=org, flock=cycle, saloon=s, recorded_by=user,
        occurred_at=now, count=250,
    )  # 2.5% — below the 3% HEALTH-RISK gate

    for _ in range(3):
        _resolved(org, tier=1, seconds=300, source="REALTIME", saloon=s)

    from apps.alerts.services.quality import alerts_per_flock

    rows = alerts_per_flock()
    assert len(rows) == 1
    row = rows[0]
    assert row["alerts"] == 3
    assert row["mortality_pct"] == pytest.approx(2.5, abs=0.3)


@pytest.mark.django_db(transaction=True)
def test_alerts_per_flock_excludes_non_realtime():
    org = _org()
    s = _saloon(org)
    now = dj_tz.now()
    cycle = FlockCycle.objects.create(
        org=org, saloon=s, breed_type="ross308", placement_count=1000,
        start_date=now - timedelta(days=10), end_date=now,
    )

    _resolved(org, tier=1, seconds=120, source="REPLAY_ANALYSIS", saloon=s)
    _resolved(org, tier=1, seconds=180, source="REPLAY_ANALYSIS", saloon=s)
    _resolved(org, tier=1, seconds=300, source="REALTIME", saloon=s)

    from apps.alerts.services.quality import alerts_per_flock

    rows = alerts_per_flock()
    assert rows[0]["alerts"] == 1


# ── snapshot() stitches every metric into one dict ──────────────────────────


@pytest.mark.django_db(transaction=True)
def test_snapshot_returns_all_keys():
    org = _org()
    user = _user(org)
    a = _resolved(org, tier=1, seconds=900)
    AlertAcknowledgement.objects.create(
        org=org,
        alert=a,
        acknowledged_by=user,
        acknowledged_at=a.triggered_at + timedelta(minutes=5),
        pause_minutes=60,
    )

    from apps.alerts.services.quality import snapshot

    snap = snapshot()
    assert "mttr_seconds_by_tier" in snap
    assert "ack_to_resolve_seconds" in snap
    assert "alerts_per_flock" in snap
    assert snap["mttr_seconds_by_tier"][1] == pytest.approx(900.0)
    # acked_mean_seconds is mean resolution time for acked alerts (only one alert: 900s)
    assert snap["ack_to_resolve_seconds"]["acked_mean_seconds"] == pytest.approx(900.0)


@pytest.mark.django_db(transaction=True)
def test_snapshot_empty_db_is_safe():
    from apps.alerts.services.quality import snapshot

    snap = snapshot()
    assert snap["mttr_seconds_by_tier"] == {}
    assert snap["ack_to_resolve_seconds"] == {"acked_mean_seconds": 0.0, "never_acked_mean_seconds": 0.0}
    assert snap["alerts_per_flock"] == []
