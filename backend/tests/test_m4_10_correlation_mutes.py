"""M4-10 acceptance — GS-CORRELATION + maintenance-mute windows.

TASKS acceptance names: test_farm_power_outage_correlation +
test_maintenance_mute_window. GS-CORRELATION (Tracker §4i): ≥
farm_power_outage_los_ratio (0.7) of a farm's ACTIVE slots LOS within
farm_power_outage_window_min (3) ⇒ ONE consolidated page +
correlation_suppress_minutes (10) paging suppression — per-sensor LOS rows
still log/alert-instance, zero schema change. GS-MAINT-MUTE: an active
maintenance_mutes window silences threshold/duration alerts ONLY; velocity
emergencies ALWAYS bypass (Decision 10); the escalation sweep writes ONE
digest line per ended window (see mutes.py for the account_activity_logs
actor-FK friction deferred to the M4-12 retrospective).

فارسی: قطعِ هم‌زمانِ بیشینهٔ اسلات‌های یک فارم = یک صفحهٔ واحد؛ پنجرهٔ تعمیر
هشدارهای آستانه‌ای را خاموش می‌کند ولی سرعتِ آمونیاک هرگز.
"""

import json
import logging
import time
import uuid

import pytest
import redis as redis_lib
from django.utils import timezone as dj_tz
from datetime import timedelta

from apps.alerts.models import AlertInstance, MaintenanceMute
from apps.alerts.services.evaluation import evaluate_entry
from apps.alerts.services.redis_keys import (
    breach_key,
    maintenance_mute_count_key,
    thresholds_hash,
)
from apps.alerts.services.sweeps import escalate_all, los_watchdog
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL  # noqa: E402

T0 = 1_000_000.0

SCRATCH_REDIS_URL = "redis://:dev_redis_password@localhost:6379/15"


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    scratch = redis_lib.Redis.from_url(SCRATCH_REDIS_URL, decode_responses=True)
    scratch.flushdb()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.alerts.services.sweeps.system_redis", lambda: client)
    monkeypatch.setattr("apps.alerts.services.mutes.system_redis", lambda: client)
    # Mature-Redis default (see test_m4_07): real container uptime would trip
    # the DR-T3 suppression check and flake every firing test.
    monkeypatch.setattr(
        client, "info", lambda section=None: {"uptime_in_seconds": 86_400}
    )
    yield client


def _make_farm_world(saloons: int = 2, slots_per_saloon: int = 2):
    """One org/farm with N saloons × M slots — the correlation ratio's stage."""
    tag = "ea" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloons_out = []
    for i in range(saloons):
        saloon = Saloon.objects.create(org=org, farm=farm, name=f"s{i}-{tag}")
        position = SaloonPosition.objects.create(
            saloon=saloon, org=org, label=f"pos-{tag}-{i}"
        )
        slots = [
            SaloonSensorSlot.objects.create(
                slot_id=f"s{i}-{j}-{tag}", saloon=saloon, org=org, position=position
            )
            for j in range(slots_per_saloon)
        ]
        GatewaySensorMapping.objects.create(
            gateway_hardware_id=f"hw-{tag}-{i}", pack_id="P-A",
            slot=slots[0], org=org,
        )
        saloons_out.append({"saloon": saloon, "slots": slots})
    return {
        "org": org, "farm": farm, "saloons": saloons_out,
        "redis": redis_lib.Redis.from_url(REDIS_URL, decode_responses=True),
    }


def _mark_online(rc, saloon, slot):
    rc.set(f"sensor:status:{saloon.id}:{slot.pk}", "ONLINE", ex=300)


def _mark_offline(rc, saloon, slot):
    rc.delete(f"sensor:status:{saloon.id}:{slot.pk}")


# ── ① GS-CORRELATION ─────────────────────────────────────────────────────────

@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_farm_power_outage_correlation(timescale_db, _fresh_redis, caplog):
    """Half the farm LOS ⇒ per-sensor pages only; ≥ 0.7 ⇒ ONE consolidated
    page + the suppression key armed for correlation_suppress_minutes."""
    world = _make_farm_world()  # 2 saloons × 2 slots = 4 active slots
    rc = _fresh_redis
    s0, s1 = world["saloons"]
    for slot in s0["slots"]:
        _mark_online(rc, s0["saloon"], slot)

    # Sweep 1: saloon 1 fully LOS → 2/4 = 0.5 < 0.7 ⇒ per-sensor pages only.
    with caplog.at_level(logging.INFO, logger="apps.alerts.services.sweeps"):
        los_watchdog()
    assert sum(1 for r in caplog.records if "FARM POWER OUTAGE" in r.getMessage()) == 0
    assert not rc.exists(f"farm:corr:muted:{world['farm'].id}")

    # Sweep 2: saloon 0 also drops → 4/4 ≥ 0.7 ⇒ ONE consolidated page.
    for slot in s0["slots"]:
        _mark_offline(rc, s0["saloon"], slot)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="apps.alerts.services.sweeps"):
        los_watchdog()
    pages = [r for r in caplog.records if "FARM POWER OUTAGE" in r.getMessage()]
    assert len(pages) == 1
    ttl = rc.ttl(f"farm:corr:muted:{world['farm'].id}")
    assert 0 < ttl <= 10 * 60
    # Per-sensor rows kept logging through BOTH sweeps (zero schema change):
    assert AlertInstance.objects.filter(
        org_id=world["org"].org_id, metric_type="los"
    ).count() == 2  # one per saloon, exactly-once each


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_correlation_suppresses_individual_paging_not_rows(timescale_db, _fresh_redis, caplog):
    """While the farm is inside its suppression window, a NEW saloon's LOS
    still creates its alert row — but pages nothing (INFO, not WARNING)."""
    world = _make_farm_world()  # 2 saloons × 2 slots
    rc = _fresh_redis
    # Farm-wide outage on sweep 1 → consolidated page + suppression armed.
    for s in world["saloons"]:
        for slot in s["slots"]:
            _mark_offline(rc, s["saloon"], slot)
    with caplog.at_level(logging.INFO, logger="apps.alerts.services.sweeps"):
        los_watchdog()
    assert any("FARM POWER OUTAGE" in r.getMessage() for r in caplog.records)

    # A brand-new saloon's episode inside the suppression window.
    saloon_c = Saloon.objects.create(
        org=world["org"], farm=world["farm"], name="c-extra"
    )
    position = SaloonPosition.objects.create(saloon=saloon_c, org=world["org"], label="px")
    slot_c = SaloonSensorSlot.objects.create(
        slot_id="c-extra-1", saloon=saloon_c, org=world["org"], position=position
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="apps.alerts.services.sweeps"):
        los_watchdog()
    # Row EXISTS (still logged), but the per-sensor PAGE is gone.
    assert AlertInstance.objects.filter(
        saloon_id=saloon_c.id, metric_type="los"
    ).exists()
    assert not any(
        "LOS ALERT [installer" in r.getMessage() for r in caplog.records
    )
    assert any("paging SUPPRESSED" in r.getMessage() for r in caplog.records)


# ── ② GS-MAINT-MUTE ──────────────────────────────────────────────────────────

def _make_mute_world():
    world = _make_farm_world(saloons=1, slots_per_saloon=1)
    rc = world["redis"]
    saloon = world["saloons"][0]["saloon"]
    rc.hset(thresholds_hash(saloon.id), mapping={
        "temp_max": "30.00", "temp_min": "18.00",
        "nh3_enabled": "1", "operational_state": "ACTIVE_FLOCK",
    })
    return world, saloon


def _evaluate(rc, world, saloon, metrics, clock=T0):
    return evaluate_entry(
        redis_client=rc,
        saloon_id=saloon.id,
        org_id=str(world["org"].org_id),
        farm_id=world["farm"].id,
        slot_id=world["saloons"][0]["slots"][0].slot_id,
        metrics=metrics,
        device_ts="2026-08-27T12:00:00Z",
        clock_now=clock,
    )


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_maintenance_mute_window(timescale_db, _fresh_redis):
    """Active mute ⇒ threshold alerting silenced (no row, no breach state);
    a velocity emergency in the SAME window still fires (Decision 10)."""
    world, saloon = _make_mute_world()
    rc = _fresh_redis
    mute = MaintenanceMute.objects.create(
        org=world["org"], saloon=saloon,
        starts_at=dj_tz.now() - timedelta(seconds=60),
        ends_at=dj_tz.now() + timedelta(seconds=600),
        reason="annual burner service",
    )

    # Threshold breach during the window → silenced entirely.
    result = _evaluate(rc, world, saloon, {"temp": {"avg": 35.0}}, clock=time.time())
    assert result is None
    assert not AlertInstance.objects.filter(
        org_id=world["org"].org_id, metric_type="temp"
    ).exists()
    assert not rc.exists(breach_key(saloon.id, "temp"))

    # Velocity emergency during the SAME window → bypasses, fires instantly.
    result = _evaluate(
        rc, world, saloon,
        {"nh3": {"avg": 90.0, "max": 100.0, "max_ts": 58}},
        clock=time.time() + 5,
    )
    assert result is not None and result["metric"] == "nh3"
    assert AlertInstance.objects.filter(
        org_id=world["org"].org_id, metric_type="nh3"
    ).exists()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_mute_digest_written_once_after_window(timescale_db, _fresh_redis, caplog):
    """The escalation sweep writes the digest for an ended window EXACTLY
    once, then clears the counter (second sweep must not re-digest)."""
    world, saloon = _make_mute_world()
    rc = _fresh_redis
    mute = MaintenanceMute.objects.create(
        org=world["org"], saloon=saloon,
        starts_at=dj_tz.now() - timedelta(minutes=30),
        ends_at=dj_tz.now() - timedelta(minutes=5),
        reason="feeder replacement",
    )
    rc.incr(maintenance_mute_count_key(mute.id))
    rc.incr(maintenance_mute_count_key(mute.id))  # 2 alerts were suppressed

    with caplog.at_level(logging.WARNING, logger="apps.alerts.services.mutes"):
        escalate_all()
        escalate_all()
    digests = [r for r in caplog.records if "MAINTENANCE MUTE DIGEST" in r.getMessage()]
    assert len(digests) == 1
    assert "2 alerts suppressed" in digests[0].getMessage()
    assert not rc.exists(maintenance_mute_count_key(mute.id))
