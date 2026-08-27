"""M4-01 acceptance — threshold hash encoding + grace + harvested state.

Canon: Walkthrough §4.1 lines 648–671 (flat fields, nh3_enabled flag,
grace-on-change only) + Q3 (HARVESTED_VACANT keeps flags/velocity armed).
Tehran-local flock-age math tested around the midnight boundary.

PG modules stay plain-function style; Redis asserts use the SERVICE's own
connection pointed at scratch DB /10 (flushed per test).
فارسی: پذیرش M4-01 — هش آستانه‌ها، کلید گذار فقط-در-تغییر، وضعیت برداشت؛
ردیسِ اختصاصی /10 برای ایزولاسیون.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone as dt_tz

import pytest
import redis as redis_lib

# ISOLATED scratch logical DB for the threshold service (flushed per test).
# Set BEFORE the service's lazy first Redis call.
SCRATCH_REDIS_URL = "redis://:dev_redis_password@localhost:6379/10"
os.environ["REDIS_URL"] = SCRATCH_REDIS_URL

from django.contrib.auth import get_user_model  # noqa: E402
from django.utils import timezone as dj_tz  # noqa: E402

from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule  # noqa: E402
from apps.alerts.services import thresholds as svc  # noqa: E402
from apps.alerts.services.clock import Clock  # noqa: E402
from apps.alerts.services.redis_keys import (  # noqa: E402
    thresholds_hash,
    transition_grace_key,
)
from apps.devices.models import SaloonSensorSlot  # noqa: E402
from apps.farms.models import Farm, Saloon  # noqa: E402
from apps.geography.models import City, Province  # noqa: E402
from apps.telemetry.models import FlockCycle  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402


def _redis():
    return svc._redis()


@pytest.fixture(autouse=True)
def _fresh_scratch_redis():
    """The docstring promised flushed-per-test. Flush the URL the SERVICE
    actually resolves (env may have been re-pointed by another test module
    at collection time) — stale hashes otherwise collide once --create-db
    resets saloon PKs and 'first write' finds an old value → phantom grace."""
    url = os.environ.get("REDIS_URL", SCRATCH_REDIS_URL)
    redis_lib.Redis.from_url(url, decode_responses=True).flushdb()
    yield


@pytest.fixture(autouse=True)
def _fresh_scratch():
    """Flush the threshold-service scratch DB before every test."""
    _redis().flushdb()
    yield


def _make_world(tag: str, start_days_ago=10):
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    template = LifecycleTemplate.objects.create(
        org=org, breed_type="ross", name=f"tpl-{tag}", is_default=True
    )
    start = dj_tz.now() - timedelta(days=start_days_ago)
    flock = FlockCycle.objects.create(
        org=org,
        saloon=saloon,
        breed_type="ross",
        start_date=start,
        placement_count=100,
    )
    return {"org": org, "saloon": saloon, "template": template, "flock": flock}


def _rule(template, metric="temp", day=10, mx="30.00", mn="18.00"):
    return LifecycleThresholdRule.objects.create(
        template=template,
        day_from=day - 2,
        day_to=day + 2,
        metric=metric,
        max_value=mx,
        min_value=mn,
    )


# ── Tehran-local flock age (pure math, no DB) ─────────────────────────────

def test_flock_age_uses_tehran_local_whole_days():
    placed = datetime(2026, 8, 25, 23, 50, tzinfo=dt_tz.utc)
    probe = datetime(2026, 8, 27, 0, 30, tzinfo=dt_tz.utc)
    assert svc.flock_age_days(placed, probe) == 1


def test_flock_age_same_moment_is_zero():
    placed = datetime(2026, 8, 25, 10, 0, tzinfo=dt_tz.utc)
    assert svc.flock_age_days(placed, placed + timedelta(hours=5)) == 0


# ── hash building (DB-backed) ─────────────────────────────────────────────

@pytest.mark.django_db
def test_hash_reflects_day_band_and_enabled_flags():
    world = _make_world("hb")
    rule = _rule(world["template"], "temp", day=10)
    nh3_rule = _rule(world["template"], "nh3", day=10)

    now_ts = dj_tz.now().timestamp()
    mapping = svc.build_hash_for_saloon(
        world["saloon"], clock=Clock(now_fn=lambda: now_ts)
    )

    assert mapping["operational_state"] == "ACTIVE_FLOCK"
    assert mapping["temp_max"] == "30.00"
    assert mapping["temp_min"] == "18.00"
    assert mapping["nh3_enabled"] == "1"
    # canon line 658 encodes ONLY nh3_enabled — no temp_enabled key exists
    assert "temp_enabled" not in mapping


@pytest.mark.django_db
def test_never_flocked_saloon_is_inert():
    world = _make_world("in")
    _rule(world["template"], "temp")
    empty = Saloon.objects.create(org=world["org"], farm=None, name="s-inert")
    assert build_hash_now(empty) is None


def build_hash_now(saloon):
    return svc.build_hash_for_saloon(saloon, clock=Clock(now_fn=lambda: dj_tz.now().timestamp()))


# ── re-cache semantics ────────────────────────────────────────────────────

@pytest.mark.django_db
def test_grace_key_only_when_values_change(timescale_db):
    tag = uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"gp-{tag}")
    city = City.objects.create(province=province, name=f"gc-{tag}")
    org = Organization.objects.create(name=f"g-org-{tag}")
    farm = Farm.objects.create(org=org, name=f"g-f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"g-s-{tag}")
    template = LifecycleTemplate.objects.create(org=org, breed_type="ross")
    start = dj_tz.now() - timedelta(days=10)

    FlockCycle.objects.create(
        org=org, saloon=saloon, breed_type="ross", start_date=start, placement_count=50
    )
    rule = LifecycleThresholdRule.objects.create(
        template=template,
        day_from=8,
        day_to=12,
        metric="temp",
        max_value="30",
        min_value="18",
    )

    # Eager post_save signals (CELERY_TASK_ALWAYS_EAGER) already re-cached on
    # every create above — canon-correct, but this test needs a controlled
    # baseline: wipe and let the EXPLICIT recache below be the first writer.
    client = _redis()
    client.flushdb()

    status1 = svc.recache_saloon(saloon.id, clock=Clock(now_fn=lambda: dj_tz.now().timestamp()))
    hash_key = thresholds_hash(saloon.id)
    grace_key = transition_grace_key(saloon.id)

    assert client.hgetall(hash_key) != {}
    assert not client.exists(grace_key)

    # identical recompute ⇒ unchanged ⇒ still no grace key
    status2 = svc.recache_saloon(saloon.id, clock=Clock(now_fn=lambda: dj_tz.now().timestamp()))
    assert status2 == "unchanged"
    assert not client.exists(grace_key)

    # Detach the fast-path signals so the EXPLICIT recache below is what
    # observes the edit (eager Celery would otherwise pre-apply it).
    # NOTE: disconnect/connect MUST repeat the registration dispatch_uid or
    # Django silently keeps the original binding.
    from django.db.models.signals import post_save

    from apps.alerts.signals import flock_saved, rules_changed
    from apps.telemetry.models import FlockCycle as _FC

    post_save.disconnect(flock_saved, sender=_FC, dispatch_uid="alerts_flock_recache")
    post_save.disconnect(rules_changed, sender=LifecycleThresholdRule, dispatch_uid="alerts_rules_recache")
    try:
        # farmer tightens the band mid-flock ⇒ REAL change ⇒ grace appears
        rule.max_value = "35.00"
        rule.save(update_fields=["max_value"])
        status3 = svc.recache_saloon(saloon.id, clock=Clock(now_fn=lambda: dj_tz.now().timestamp()))
        assert status3 == "changed"
        assert client.exists(grace_key)
    finally:
        post_save.connect(flock_saved, sender=_FC, dispatch_uid="alerts_flock_recache")
        post_save.connect(rules_changed, sender=LifecycleThresholdRule, dispatch_uid="alerts_rules_recache")


# ── harvested state (Q3) ──────────────────────────────────────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_flock_end_flips_state_keeps_flags(seeded_rls_db):
    tag = uuid.uuid4().hex[:6]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    template = LifecycleTemplate.objects.create(org=org, breed_type="ross")
    start = dj_tz.now() - timedelta(days=10)

    FlockCycle.objects.create(
        org=org, saloon=saloon, breed_type="ross", start_date=start, placement_count=50
    )
    LifecycleThresholdRule.objects.create(
        template=template,
        day_from=8,
        day_to=12,
        metric="nh3",
        max_value="20",
        min_value="5",
    )

    status1 = svc.recache_saloon(
        saloon.id, clock=Clock(now_fn=lambda: dj_tz.now().timestamp())
    )
    assert status1 in ("changed", "unchanged")

    # flock END flips operational state while keeping flags intact (Q3)
    flock = FlockCycle.objects.get(saloon_id=saloon.id)
    flock.end_date = dj_tz.now()
    flock.save(update_fields=["end_date"])

    status2 = svc.recache_saloon(
        saloon.id, clock=Clock(now_fn=lambda: dj_tz.now().timestamp())
    )
    mapping = build_hash_now(saloon)

    assert mapping["operational_state"] == "HARVESTED_VACANT"
    assert mapping["nh3_enabled"] == "1"


def build_hash_now(saloon):
    return svc.build_hash_for_saloon(saloon, clock=Clock(now_fn=lambda: dj_tz.now().timestamp()))
