"""M4-06 acceptance â€” ack mechanism: row + pause key in ONE transaction.

TASKS acceptance name: test_alert_acknowledgement_pause. The REST endpoint
is v0.6 surface (M6-01); here the SERVICE is the mechanism. Sweeps honor the
pause already (M4-05): skipped alerts are neither promoted NOR resolved while
the key lives. Expiry simulation follows house practice â€” deleting the key IS
time travel for a TTL.
ÙØ§Ø±Ø³ÛŒ: Ù¾Ø°ÛŒØ±Ø´ M4-06 â€” Ø±Ø³ÛŒØ¯Ù‡â€ŒØ³Ø§Ø²ÛŒ Ø±Ø¯ÛŒÙ Ùˆ Ú©Ù„ÛŒØ¯Ù ØªÙˆÙ‚Ù Ø±Ø§ ÛŒÚ©Ø¬Ø§ Ù…ÛŒâ€ŒÙ†ÙˆÛŒØ³Ø¯Ø› Ú†Ø±Ø®Ø´Ù Ø§Ø±ØªÙ‚Ø§
ØªØ§ Ù¾Ø§ÛŒØ§Ù†Ù ØªÙˆÙ‚Ù Ú©Ø§Ù…Ù„ Ù…ÛŒâ€ŒØ§ÛŒØ³ØªØ¯ Ùˆ Ø¨Ø§ Ø§Ù†Ù‚Ø¶Ø§ Ø¯Ù‚ÛŒÙ‚Ø§Ù‹ Ø§Ø¯Ø§Ù…Ù‡ Ù…ÛŒâ€ŒÛŒØ§Ø¨Ø¯.
"""

import time
import uuid
from datetime import timedelta

import pytest
import redis as redis_lib

from apps.alerts.models import AlertAcknowledgement, AlertInstance
from apps.alerts.services.ack import CONFIG_KEY, acknowledge_alert, pause_minutes
from apps.alerts.services.redis_keys import breach_key, paused_key, thresholds_hash
from apps.alerts.services.sweeps import escalate_all
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.system_config.models import SystemConfiguration
from apps.tenants.models import Organization

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL, redis_base_url  # noqa: E402

SCRATCH_REDIS_URL = redis_base_url() + "/10"


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    """House standard (ade9e18): same integration client injected everywhere â€”
    sweeps AND the ack service â€” plus a scratch db flush per test."""
    scratch = redis_lib.Redis.from_url(SCRATCH_REDIS_URL, decode_responses=True)
    scratch.flushdb()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    monkeypatch.setattr("apps.alerts.services.sweeps.system_redis", lambda: client)
    yield client


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_alert_acknowledgement_pause(timescale_db, django_user_model, _fresh_redis):
    """Full acceptance: ack row + pause key appear together; the sweep freezes
    completely while paused (no Tier 2/3 promotion); expiry releases ALL
    accrued escalation at once (never demotes â€” 40 min held â‡’ straight to
    Tier 3)."""
    world = _make_world(_fresh_redis)
    user = django_user_model.objects.create_user(
        username=f"eng-{world['tag']}", password="strong-pass-123"
    )

    # Open Tier-1 episode: row freshly fired, breach still live.
    now = time.time()
    alert = AlertInstance.objects.create(
        org=world["org"], farm=world["saloon"].farm, saloon=world["saloon"],
        metric_type="temp", current_status="ACTIVE_TIER_1",
        current_value=33.5, source="REALTIME",
    )
    _fresh_redis.set(
        breach_key(world["saloon"].id, "temp"),
        f'{{"started_at": {now - 60.0}, "alerted": true, "value": 33.5}}',
        ex=840,
    )

    ack = acknowledge_alert(alert, user, notes="checked the sensor line")

    # â”€â”€ Row: right user/org/alert, minutes taken FROM CONFIGURATION â”€â”€â”€â”€â”€â”€â”€â”€
    row = AlertAcknowledgement.objects.get(pk=ack.pk)
    assert row.alert_id == alert.id
    assert row.org_id == world["org"].org_id
    assert row.acknowledged_by_id == user.pk
    assert row.notes == "checked the sensor line"
    assert row.pause_minutes == pause_minutes() == 10  # seeded value (0003)

    # â”€â”€ Pause key: present, TTL bounded by config minutes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    pkey = paused_key(str(alert.id))
    assert _fresh_redis.exists(pkey)
    assert 0 < _fresh_redis.ttl(pkey) <= row.pause_minutes * 60

    # â”€â”€ While paused: timer frozen past BOTH tier marks (aged 40 min) â”€â”€â”€â”€â”€â”€
    _age(alert, minutes=40)
    assert escalate_all() == 0
    alert.refresh_from_db()
    assert alert.current_status == "ACTIVE_TIER_1"

    # â”€â”€ Expiry (= delete): accrued escalation lands AT ONCE â†’ Tier 3 â”€â”€â”€â”€â”€â”€â”€
    _fresh_redis.delete(pkey)
    assert escalate_all() >= 1
    alert.refresh_from_db()
    assert alert.current_status == "ACTIVE_TIER_3"
    assert alert.highest_tier_reached == 3


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_re_acknowledge_refreshes_pause(timescale_db, django_user_model, _fresh_redis):
    """A second human ack extends the pause: another audit row, TTL renewed."""
    world = _make_world(_fresh_redis)
    user = django_user_model.objects.create_user(
        username=f"mgr-{world['tag']}", password="strong-pass-123"
    )
    alert = AlertInstance.objects.create(
        org=world["org"], farm=world["saloon"].farm, saloon=world["saloon"],
        metric_type="nh3", current_status="ACTIVE_TIER_2",
        current_value=42.0, source="REALTIME", highest_tier_reached=2,
    )
    pkey = paused_key(str(alert.id))

    acknowledge_alert(alert, user, redis_client=_fresh_redis)
    _fresh_redis.expire(pkey, 120)  # pretend most of the pause already burned
    first_ttl = _fresh_redis.ttl(pkey)

    acknowledge_alert(alert, user, notes="still on site", redis_client=_fresh_redis)

    assert AlertAcknowledgement.objects.filter(alert=alert).count() == 2
    refreshed = _fresh_redis.ttl(pkey)
    assert refreshed > first_ttl  # renewed to ~10 min again
    assert 400 < refreshed <= 600


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_pause_length_follows_config(timescale_db, django_user_model, _fresh_redis):
    """Audit-M5 clause made real: raise the config â‡’ next ack pauses longer."""
    world = _make_world(_fresh_redis)
    user = django_user_model.objects.create_user(
        username=f"dr-{world['tag']}", password="strong-pass-123"
    )
    SystemConfiguration.objects.filter(key=CONFIG_KEY).update(value="25")
    try:
        alert = AlertInstance.objects.create(
            org=world["org"], farm=world["saloon"].farm, saloon=world["saloon"],
            metric_type="humidity", current_status="ACTIVE_TIER_1",
            current_value=88.0, source="REALTIME",
        )
        ack = acknowledge_alert(alert, user, redis_client=_fresh_redis)
        assert ack.pause_minutes == 25
        assert _fresh_redis.ttl(paused_key(str(alert.id))) > 20 * 60
    finally:
        SystemConfiguration.objects.filter(key=CONFIG_KEY).update(value="10")


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _make_world(client):
    tag = "e6" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"f-org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position
    )
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot, org=org
    )
    client.hset(thresholds_hash(saloon.id), mapping={
        "temp_max": "30.00", "temp_min": "18.00", "operational_state": "ACTIVE_FLOCK",
    })
    return {"org": org, "saloon": saloon, "slot": slot, "tag": tag}


def _age(alert, minutes):
    """Backdate triggered_at so the sweep's clock math sees an aged alert."""
    AlertInstance.objects.filter(pk=alert.pk).update(
        triggered_at=alert.triggered_at - timedelta(minutes=minutes),
    )
    alert.refresh_from_db()

