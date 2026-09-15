"""BUG-9 residual proof — sweeps work under ENFORCED RLS as the app role.

The original BUG-9 disease: background jobs ran context-free reads on the
default alias with no tenant_context, so as morghdari_app under FORCE RLS
they saw ZERO rows and silently did nothing (QA 8.4b/9.x/10.9/20.3a). The
fix routes cross-org discovery through the sanctioned admin alias
(BYPASSRLS in prod; a second superuser connection in CI) while per-org
reads/writes stay inside tenant_context on the default alias.

Each test seeds rows as the bootstrap superuser, then SET ROLEs to
morghdari_app with NO org GUC and runs the job — it must still find and
process rows. Pattern follows tests/test_rls.py (SET ROLE / RESET ROLE in
finally); markers replicate the neighboring sweep suites
(test_m4_05_escalation / test_m4_07_los / test_m4_08_replay):
use_migrations + django_db(transaction=True) + timescale_db.

CI note: docker-compose.ci.yml provides no DATABASE_ADMIN_URL, so
_admin_alias() falls back to "default" — the SAME session that SET ROLE
just blinded. _ensure_admin_alias() below materialises a second superuser
connection named "admin" (copy of default settings) so discovery rides a
session that never received SET ROLE, exactly emulating prod's
app-role-default + BYPASSRLS-admin topology. Without it all four tests
fail vacuously with zero-row reads even though prod is fixed.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
import redis as redis_lib
from django.db import connection, connections
from django.utils import timezone as dj_tz

from apps.alerts.models import AlertInstance, MaintenanceMute
from apps.alerts.services.mutes import write_maintenance_mute_digests
from apps.alerts.services.redis_keys import (
    breach_key,
    maintenance_mute_count_key,
    sensor_status_key,
    thresholds_hash,
)
from apps.alerts.services.replay import run_replay_sweep
from apps.alerts.services.sweeps import escalate_all, los_watchdog
from apps.devices.models import SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry.models import SaloonTelemetry
from apps.tenants.models import Organization

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL, redis_base_url  # noqa: E402

# Fixture-scoped admin alias lifecycle (Attempt 2: module-scoped autouse).
# Replaces the old import-time settings.DATABASES["admin"] mutation (which
# leaked session-wide via pytest's collection-time import and flipped every
# product _admin_alias() to "admin", killing ~47 modules with
# DatabaseOperationForbidden). Attempt 1 (function-scoped autouse) FAILED:
# pytest-django's _django_db_marker → _django_db_helper →
# TransactionTestCase.setUpClass → _validate_databases runs BEFORE the
# function fixture registers "admin", raising ImproperlyConfigured
# ("'admin' which is not defined in settings.DATABASES"). Module scope runs
# before any function-scoped test setup, so registration precedes validation.
# Registration happens at fixture setup (after django_db_setup already
# created the default test DB — copying default's settings then points at the
# SAME physical test DB, so no separate creation is needed) and teardown
# CLOSES the connection and DELETES the alias from both dicts, restoring the
# pre-test state for all other suites.
@pytest.fixture(scope="module", autouse=True)
def _rls_admin_alias():
    from django.conf import settings as _s

    from django.db import connections as _conns

    from tests.conftest import _install_cascade_flush

    if "admin" not in _s.DATABASES:
        _s.DATABASES["admin"] = dict(_s.DATABASES["default"])
    # connections.settings IS settings.DATABASES (same object) after first
    # access, but set explicitly for handlers that snapshotted earlier.
    try:
        if "admin" not in _conns.settings:
            _conns.settings["admin"] = dict(_conns.settings["default"])
    except Exception:
        pass
    _install_cascade_flush(_conns["admin"])
    yield
    try:
        _conns["admin"].close()
    except Exception:
        pass
    try:
        del _conns["admin"]
    except Exception:
        pass
    try:
        _s.DATABASES.pop("admin", None)
    except Exception:
        pass
    try:
        _conns.settings.pop("admin", None)
    except Exception:
        pass


SCRATCH_REDIS_URL = redis_base_url() + "/9"


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    """House standard (ade9e18): NO env mutation — inject THE SAME
    integration client into every service that lazily builds one, plus a
    mature-Redis uptime so the DR-T3 suppression never flakes firing tests."""
    scratch = redis_lib.Redis.from_url(SCRATCH_REDIS_URL, decode_responses=True)
    scratch.flushdb()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    monkeypatch.setattr("apps.alerts.services.sweeps.system_redis", lambda: client)
    monkeypatch.setattr("apps.alerts.services.mutes.system_redis", lambda: client)
    monkeypatch.setattr("apps.alerts.services.replay.system_redis", lambda: client)
    monkeypatch.setattr(
        client, "info", lambda section=None: {"uptime_in_seconds": 86_400}
    )
    yield client


def _make_org_farm_saloon(tag: str):
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    return org, farm, saloon


def _make_slot(org, saloon, tag: str) -> SaloonSensorSlot:
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label=f"pos-{tag}")
    return SaloonSensorSlot.objects.create(
        slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position
    )


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True, databases=["default", "admin"])
def test_escalate_finds_rows_as_app_role(timescale_db, seeded_rls_db, _fresh_redis):
    """escalate_all() promotes an unresolved breach alert as morghdari_app
    with no org GUC (old code saw ZERO rows here)."""
    tag = "rlsesc" + uuid.uuid4().hex[:8]
    org, farm, saloon = _make_org_farm_saloon(tag)
    rc = _fresh_redis
    rc.hset(thresholds_hash(saloon.id), mapping={
        "temp_max": "30.00", "temp_min": "18.00",
        "nh3_enabled": "1", "operational_state": "ACTIVE_FLOCK",
    })
    alert = AlertInstance.objects.create(
        org=org, farm=farm, saloon=saloon,
        metric_type="temp", current_status="ACTIVE_TIER_1",
        current_value=32.00, source="REALTIME",
    )
    AlertInstance.objects.filter(pk=alert.pk).update(
        triggered_at=alert.triggered_at - timedelta(minutes=16),
    )
    alert.refresh_from_db()
    rc.set(
        breach_key(saloon.id, "temp"),
        json.dumps({"started_at": time.time() - 3600.0, "alerted": True, "value": 32.0}),
        ex=900,
    )
    with connection.cursor() as c:
        c.execute("SET ROLE morghdari_app")
    try:
        changed = escalate_all()
        assert changed >= 1
    finally:
        with connection.cursor() as c:
            c.execute("RESET ROLE")
    alert.refresh_from_db()
    assert alert.current_status == "ACTIVE_TIER_2"
    assert alert.highest_tier_reached == 2


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True, databases=["default", "admin"])
def test_los_watchdog_fires_as_app_role(timescale_db, seeded_rls_db, _fresh_redis):
    """los_watchdog() fires on a missing sensor:status key as the app role
    with no org GUC (old code saw ZERO slots here)."""
    tag = "rlslos" + uuid.uuid4().hex[:8]
    org, farm, saloon = _make_org_farm_saloon(tag)
    slot_a = _make_slot(org, saloon, f"a-{tag}")
    slot_b = _make_slot(org, saloon, f"b-{tag}")
    rc = _fresh_redis
    rc.set(sensor_status_key(saloon.id, slot_a.pk), "ONLINE", ex=300)
    # slot_b never checked in — its key is expired, the saloon is in LOS.
    with connection.cursor() as c:
        c.execute("SET ROLE morghdari_app")
    try:
        fired = los_watchdog()
        assert fired == 1
    finally:
        with connection.cursor() as c:
            c.execute("RESET ROLE")
    assert AlertInstance.objects.filter(
        saloon_id=saloon.id, metric_type="los", resolved_at__isnull=True
    ).count() == 1


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True, databases=["default", "admin"])
def test_mute_digest_emitted_as_app_role(
    timescale_db, seeded_rls_db, _fresh_redis, caplog
):
    """write_maintenance_mute_digests() emits the digest line for an ended
    window as the app role with no org GUC (old code saw ZERO mutes here)."""
    tag = "rlsmute" + uuid.uuid4().hex[:8]
    org, farm, saloon = _make_org_farm_saloon(tag)
    rc = _fresh_redis
    mute = MaintenanceMute.objects.create(
        org=org, saloon=saloon,
        starts_at=dj_tz.now() - timedelta(minutes=30),
        ends_at=dj_tz.now() - timedelta(minutes=5),
        reason="feeder replacement",
    )
    rc.incr(maintenance_mute_count_key(mute.id))
    rc.incr(maintenance_mute_count_key(mute.id))
    with connection.cursor() as c:
        c.execute("SET ROLE morghdari_app")
    try:
        with caplog.at_level(logging.WARNING, logger="apps.alerts.services.mutes"):
            digested = write_maintenance_mute_digests()
        assert digested == [mute.id]
    finally:
        with connection.cursor() as c:
            c.execute("RESET ROLE")
    digests = [r for r in caplog.records if "MAINTENANCE MUTE DIGEST" in r.getMessage()]
    assert len(digests) == 1
    assert "2 alerts suppressed" in digests[0].getMessage()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True, databases=["default", "admin"])
def test_replay_sweep_returns_batch_as_app_role(timescale_db, seeded_rls_db, _fresh_redis):
    """run_replay_sweep() returns the batch for buffered rows as the app role
    with no org GUC (old code saw ZERO buffered rows here)."""
    tag = "rlsrep" + uuid.uuid4().hex[:8]
    org, farm, saloon = _make_org_farm_saloon(tag)
    slot = _make_slot(org, saloon, tag)
    rc = _fresh_redis
    rc.hset(thresholds_hash(saloon.id), mapping={
        "temp_max": "30.00", "temp_min": "18.00",
        "nh3_enabled": "1", "operational_state": "ACTIVE_FLOCK",
    })
    base = datetime(2026, 8, 25, 6, 0, tzinfo=dt_timezone.utc)
    for minute, value in ((0, 32.0), (1, 33.5)):
        ts = base + timedelta(minutes=minute)
        SaloonTelemetry.objects.create(
            org=org, saloon=saloon, slot=slot,
            device_ts=ts, server_received_ts=ts,
            metrics={"temp": {"avg": value}},
            is_buffered=True,
        )
    with connection.cursor() as c:
        c.execute("SET ROLE morghdari_app")
    try:
        created = run_replay_sweep()
        assert len(created) >= 1
        assert any(slot.slot_id in batch for batch in created)
    finally:
        with connection.cursor() as c:
            c.execute("RESET ROLE")
    assert AlertInstance.objects.filter(
        org_id=org.org_id, source="REPLAY_ANALYSIS"
    ).count() == 1
