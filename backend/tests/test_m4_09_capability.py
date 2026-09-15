"""M4-09 acceptance — D23 capability-gated rules: THI + co/power_* sources.

TASKS acceptance (rewritten by Tracker §4l-Q5): engine-local named tests green
— THI derived-alert + capability-gated co/power sources; the canonical
`test_capability_profile` stays M6. D23 (Saas.md): missing metric ⇒ rule
UNAVAILABLE, never silently normal. THI is DERIVED from co-present temp +
humidity (no metric row); co/power rules flow through the normal hash path
and evaluate only when an entry actually carries the key.

فارسی: پذیرش M4-09 — هشدار THI فقط با وجود هم‌زمانِ دما و رطوبت؛ قواعدِ
co/power فقط وقتی سنسورِ زنده داشته باشند سنجیده می‌شوند.
"""

import json
import uuid

import pytest
import redis as redis_lib

from apps.alerts.models import AlertInstance
from apps.alerts.services.capability import compute_thi
from apps.alerts.services.evaluation import evaluate_entry
from apps.alerts.services.redis_keys import breach_key, thresholds_hash
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL  # noqa: E402

T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client


def _make_world():
    tag = "e9" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"a-{tag}", saloon=saloon, org=org, position=position
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot, org=org
    )
    return {"org": org, "farm": farm, "saloon": saloon, "slot": slot}


def _seed_thresholds(rc, saloon, extra=None):
    mapping = {
        "temp_max": "30.00", "temp_min": "18.00",
        "nh3_enabled": "1", "operational_state": "ACTIVE_FLOCK",
    }
    if extra:
        mapping.update(extra)
    rc.hset(thresholds_hash(saloon.id), mapping=mapping)


def _seed_sustained_breach(rc, saloon, metric, value, clock=T0):
    """Open a breach episode 901 s ago (duration gate met, not yet fired)."""
    rc.set(
        breach_key(saloon.id, metric),
        json.dumps({"started_at": clock - 901.0, "alerted": False, "value": value}),
        ex=900,
    )


def _evaluate(rc, world, metrics, clock=T0):
    return evaluate_entry(
        redis_client=rc,
        saloon_id=world["saloon"].id,
        org_id=str(world["org"].org_id),
        farm_id=world["farm"].id,
        slot_id=world["slot"].slot_id,
        metrics=metrics,
        device_ts="2026-08-27T12:00:00Z",
        clock_now=clock,
    )


# ── ① the formula is pinned ──────────────────────────────────────────────────

def test_compute_thi_known_values():
    """NRC form, T °C + %RH — pinned so retrospective ratification (M4-12)
    reviews a CONSTANT, not a moving target."""
    assert abs(compute_thi(30.0, 50.0) - 78.3) < 1e-9
    assert abs(compute_thi(35.0, 70.0) - 88.895) < 1e-9


# ── ② THI derived-alert (both inputs live) ───────────────────────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_thi_derived_alert_fires_when_inputs_copresent(timescale_db, _fresh_redis):
    world = _make_world()
    _seed_thresholds(_fresh_redis, world["saloon"], {"thi_max": "78"})
    thi = compute_thi(35.0, 70.0)  # ≈ 88.9 — sustained heat stress
    _seed_sustained_breach(_fresh_redis, world["saloon"], "thi", thi)

    result = _evaluate(
        _fresh_redis, world,
        {"temp": {"avg": 35.0}, "humidity": {"avg": 70.0}},
    )
    assert result is not None and result["metric"] == "thi"
    alert = AlertInstance.objects.get(
        org_id=world["org"].org_id, metric_type="thi",
        resolved_at__isnull=True,
    )
    assert alert.source == "REALTIME"
    assert abs(float(alert.current_value) - thi) < 0.01  # NUMERIC(10,2) column


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_thi_unavailable_without_humidity(timescale_db, _fresh_redis):
    """D23 gate: humidity missing ⇒ THI is UNAVAILABLE — never derived from
    one input, never assumed normal, no phantom breach state."""
    world = _make_world()
    _seed_thresholds(_fresh_redis, world["saloon"], {"thi_max": "78"})

    result = _evaluate(_fresh_redis, world, {"temp": {"avg": 35.0}})
    assert result is None
    assert not AlertInstance.objects.filter(
        org_id=world["org"].org_id, metric_type="thi"
    ).exists()
    # No half-derived breach episode may linger for the missing metric.
    assert not _fresh_redis.exists(breach_key(world["saloon"].id, "thi"))


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_thi_rule_absent_means_no_derivation(timescale_db, _fresh_redis):
    """No thi_max in the hash ⇒ the saloon hasn't subscribed to THI — the
    engine must not invent a threshold."""
    world = _make_world()
    _seed_thresholds(_fresh_redis, world["saloon"])  # no thi_max

    result = _evaluate(
        _fresh_redis, world,
        {"temp": {"avg": 35.0}, "humidity": {"avg": 70.0}},
    )
    assert result is None
    assert not AlertInstance.objects.filter(
        org_id=world["org"].org_id, metric_type="thi"
    ).exists()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_thi_muted_suppresses_without_crashing(timescale_db, _fresh_redis):
    """BUG-20 (QA 8.13): a muted 'thi' category with co-present temp +
    humidity used to raise NameError — `thi` was only computed inside the
    unmuted else-branch while track_breach referenced it either way, so
    every THI-capable reading crashed the drain task. Muted must mean:
    note + skip (same semantics as lane ③), never a crash, never a
    phantom breach episode, digest still fed (D24-6)."""
    import datetime as dt

    from apps.accounts.models import User
    from apps.alerts.models import AlertSubscription

    world = _make_world()
    _seed_thresholds(_fresh_redis, world["saloon"], {"thi_max": "78"})
    user = User.objects.create_user(username=f"mu-{uuid.uuid4().hex[:8]}")
    AlertSubscription.objects.create(
        org=world["org"], metric_key="thi", muted_by=user, reason="qa bug-20"
    )
    thi = compute_thi(35.0, 70.0)  # ≈ 88.9 — would fire were it not muted
    _seed_sustained_breach(_fresh_redis, world["saloon"], "thi", thi)

    result = _evaluate(
        _fresh_redis, world,
        {"temp": {"avg": 35.0}, "humidity": {"avg": 70.0}},
    )
    assert result is None
    assert not AlertInstance.objects.filter(
        org_id=world["org"].org_id, metric_type="thi"
    ).exists()
    # the suppressed fire fed the digest (D24-6 mitigation)
    tehran = dt.timezone(dt.timedelta(hours=3, minutes=30))
    day = dt.datetime.now(tehran).date().isoformat()
    digest = _fresh_redis.get(f"submuted:{world['org'].org_id}:thi:{day}")
    assert int(digest or 0) >= 1


# ── ③ capability-gated co / power sources ────────────────────────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_co_rule_evaluates_only_with_live_source(timescale_db, _fresh_redis):
    """co_max in the hash is INERT while the saloon sends no `co` key (no
    sensor); the moment a live co reading arrives the rule arms — and after
    the duration gate it fires exactly like any threshold metric."""
    world = _make_world()
    _seed_thresholds(_fresh_redis, world["saloon"], {"co_max": "50"})

    # No co sensor → no co key in readings → rule stays silent (unavailable).
    _evaluate(_fresh_redis, world, {"temp": {"avg": 25.0}})
    assert not _fresh_redis.exists(breach_key(world["saloon"].id, "co"))

    # CO sensor goes live and breaches → the same rule now tracks.
    _evaluate(
        _fresh_redis, world,
        {"temp": {"avg": 25.0}, "co": {"avg": 80.0}}, clock=T0 + 10,
    )
    assert _fresh_redis.exists(breach_key(world["saloon"].id, "co"))

    # Sustained co breach (episode 901 s old) → fires with the worst value.
    _seed_sustained_breach(_fresh_redis, world["saloon"], "co", 80.0, clock=T0 + 10)
    result = _evaluate(
        _fresh_redis, world,
        {"temp": {"avg": 25.0}, "co": {"avg": 82.0}}, clock=T0 + 20,
    )
    assert result is not None and result["metric"] == "co"
    assert AlertInstance.objects.filter(
        org_id=world["org"].org_id, metric_type="co"
    ).exists()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_power_source_gated_the_same_way(timescale_db, _fresh_redis):
    """power_mains follows the identical gate: no key in the reading ⇒ no
    evaluation, no state, no assumed-normal (D23 degradation rule)."""
    world = _make_world()
    _seed_thresholds(_fresh_redis, world["saloon"], {"power_mains_max": "10"})

    _evaluate(_fresh_redis, world, {"temp": {"avg": 25.0}})
    assert not _fresh_redis.exists(breach_key(world["saloon"].id, "power_mains"))
    assert not AlertInstance.objects.filter(
        org_id=world["org"].org_id, metric_type="power_mains"
    ).exists()
