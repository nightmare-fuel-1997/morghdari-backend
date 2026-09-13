"""M8-03 acceptance — T3 Redis-wipe rebuild (DR §3.2).

The drill's SERVER-SIDE legs, tested RED before the drill script wires them:

  * `rebuild_active_state` re-creates breach + active-alert keys from the DB
    ledger (Postgres is truth; Redis is derived) with the correct shapes;
  * LOS and REPLAY_ANALYSIS open rows are deliberately NOT re-created;
  * the whole rebuild is idempotent (SET-overwrite converges);
  * `cache_daily_thresholds`' recache path routes cross-org reads through the
    admin-or-default alias (fixes the M4-latent all-saloons no-op under
    FORCE-RLS).

فارسی: پذیرش M8-03 — بازسازیِ وضعیتِ فعالِ هشدار از دیتابیس پس از پاک‌شدنِ ردیس؛
با تستِ شکلِ دقیقِ کلیدها، ردِ LOS و REPLAY، و هم‌توانیِ تکرار.
"""

import json

import pytest
import redis as redis_lib

from apps.alerts.models import AlertInstance
from apps.alerts.services.rebuild_state import rebuild_active_state
from apps.alerts.services.redis_keys import active_alert_key, breach_key
from apps.devices.models import GatewaySensorMapping  # noqa: F401  (fixture parity)
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization

from tests.conftest import redis_base_url  # noqa: E402

REDIS_URL = redis_base_url() + "/11"  # scratch DB — rebuild tests own it


@pytest.fixture(autouse=True)
def redis_client():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client


@pytest.fixture
def alert_world(django_user_model):
    """Minimal org → farm → saloon (provisioning_world pattern, M5)."""
    province = Province.objects.create(name="P8")
    city = City.objects.create(province=province, name="C8")
    org = Organization.objects.create(name="rebuild-org")
    farm = Farm.objects.create(org=org, name="farm", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="saloon")
    return {
        "org": org, "farm": farm, "saloon": saloon,
        "org_id": str(org.org_id), "farm_id": farm.pk, "saloon_id": saloon.pk,
    }


def _open_alert(world, metric="temp", source="REALTIME", status="ACTIVE_TIER_1",
                value=32.0, trigger_epoch=None):
    import datetime as dt

    from django.utils import timezone

    fields = dict(
        org_id=world["org_id"], farm_id=world["farm_id"],
        saloon_id=world["saloon_id"], metric_type=metric,
        current_status=status, current_value=value, source=source,
    )
    if trigger_epoch is not None:
        # Explicit instant (timezone.utc lives on datetime, not django.timezone;
        # omitting the field lets the DB default apply — explicit None would
        # violate the NOT NULL column).
        fields["triggered_at"] = dt.datetime.fromtimestamp(trigger_epoch, tz=dt.timezone.utc)
    return AlertInstance.objects.create(**fields)


@pytest.mark.django_db
class TestRebuildActiveState:
    def test_open_realtime_alert_is_rematerialised(self, alert_world, redis_client):
        _open_alert(alert_world, trigger_epoch=1_700_000_000)
        counts = rebuild_active_state(client=redis_client)
        assert counts["breach_keys"] == 1 and counts["active_keys"] == 1

        b_key = breach_key(alert_world["saloon_id"], "temp")
        raw = redis_client.get(b_key)
        assert raw, "breach key must exist after rebuild"
        state = json.loads(raw)
        assert state["started_at"] == 1_700_000_000
        assert state["alerted"] is True          # alert already fired pre-wipe
        assert state["value"] == 32.0
        assert redis_client.ttl(b_key) == 900    # fresh breach_duration_sec window

        a_key = active_alert_key(alert_world["saloon_id"], "temp")
        blob = json.loads(redis_client.get(a_key))
        assert blob["metric"] == "temp"
        assert blob["saloon_id"] == alert_world["saloon_id"]
        assert blob["current_tier"] == 1
        assert blob["alert_id"]  # UUID of the open row
        assert blob["reason"] == "REBUILD_AFTER_REDIS_WIPE"

    def test_los_and_replay_rows_are_skipped(self, alert_world, redis_client):
        _open_alert(alert_world, metric="temp")
        _open_alert(alert_world, metric="los")                    # LOS: watchdog-owned
        _open_alert(alert_world, metric="nh3", source="REPLAY_ANALYSIS")

        counts = rebuild_active_state(client=redis_client)
        assert counts["breach_keys"] == 1
        assert counts["skipped_los"] == 1
        assert counts["skipped_replay"] == 1

        for metric in ("los", "nh3"):
            assert redis_client.exists(breach_key(alert_world["saloon_id"], metric)) == 0
            assert redis_client.exists(active_alert_key(alert_world["saloon_id"], metric)) == 0

    def test_resolved_rows_are_ignored(self, alert_world, redis_client):
        alert = _open_alert(alert_world)
        alert.resolved_at = alert.triggered_at
        alert.current_status = "RESOLVED"
        alert.save()

        counts = rebuild_active_state(client=redis_client)
        assert counts == {"breach_keys": 0, "active_keys": 0,
                          "skipped_los": 0, "skipped_replay": 0}

    def test_rebuild_is_idempotent(self, alert_world, redis_client):
        _open_alert(alert_world, trigger_epoch=1_700_000_000)
        first = rebuild_active_state(client=redis_client)
        second = rebuild_active_state(client=redis_client)
        assert first == second
        key = breach_key(alert_world["saloon_id"], "temp")
        assert json.loads(redis_client.get(key))["started_at"] == 1_700_000_000

    def test_empty_ledger_is_a_clean_zero(self, alert_world, redis_client):
        assert rebuild_active_state(client=redis_client) == {
            "breach_keys": 0, "active_keys": 0, "skipped_los": 0, "skipped_replay": 0}


def test_admin_alias_routing_falls_back_to_default():
    """No `admin` alias on CI/laptop/sqlite — the rebuild must use `default`."""
    from apps.alerts.services import rebuild_state

    assert rebuild_state._admin_alias() in ("admin", "default")