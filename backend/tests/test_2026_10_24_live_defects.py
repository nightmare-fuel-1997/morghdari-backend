"""Regression tests for the 2026-10-24 live-verification defect batch.

Every test here pins a defect the clean-slate twin sweep found on the test
server — defects the existing suite missed because it exercised the modules
directly instead of the live HTTP/MQTT paths:

D1  MFA first-run deadlock: /auth/mfa/setup|confirm demanded a JWT that only
    a completed MFA login could mint. Fixed: password admission for accounts
    with no CONFIRMED enrollment; confirmed accounts still demand the JWT.
D2  /installations/assignable/ 500'd on a non-UUID org_id (ValidationError
    leaked). Fixed: 400.
D3  POST /gateways/{id}/commands/ accepted kind=ROTATE_CREDENTIAL it could
    never fulfil (no staged hash) — the device correctly FAILED it. Fixed:
    400 with a pointer to the dedicated endpoint.
D4  reconcile_reported's FAILED branch failed EVERY in-flight command on the
    slot and stamped applied_at on them (a FAILED row with applied_at is a
    lie). Fixed: PROVISION-kind only, no applied_at.
D4b the command-expiry sweep could expire a row whose applied_at was set.
    Fixed: applied_at__isnull=True on both queries.
D5  the celery threshold recache ran under FORCE-RLS with no GUC and saw zero
    lifecycle_templates — assigned bands never reached the hash. Fixed:
    template reads on the sanctioned admin alias.
D6  the subscriber writes FLAT metrics ({"temp": 25.75}) but evaluate_entry
    required {"temp": {"avg": ...}} — threshold alerting was dead for all
    live telemetry. Fixed: normalization at the entry point.
D7  metric_hourly_aggregates had NO writer anywhere — charts, KPI lane and
    the daily rollup all read an always-empty table. Fixed: build_metric_hourly.
"""

import datetime as dt

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.alerts.services.evaluation import evaluate_entry
from apps.accounts import mfa
from apps.provisioning.api import issue_gateway_command
from apps.provisioning.installations import install_assignable
from apps.provisioning.models import GatewayCommand
from apps.provisioning.tasks import reconcile_reported

from tests.test_m11_03_installation_jobs import _staff, _world

# NOTE: deliberately NOT a module-level django_db marker — the flat-metrics
# evaluation test is pure Redis and must not touch the DB fixture chain.
pytestmark = []


# ---------------------------------------------------------------------------
# D1 — the first-run MFA path
# ---------------------------------------------------------------------------

def _mfa_call(view, data, user=None):
    request = APIRequestFactory().post("/x", data=data, format="json")
    if user is not None:
        force_authenticate(request, user=user)
    return view(request)


@pytest.mark.django_db
def test_mfa_setup_password_path_issues_a_secret(django_user_model):
    from apps.accounts.views import mfa_setup

    _staff(django_user_model, "mfa-fresh-2", "support")
    response = _mfa_call(
        mfa_setup, {"username": "mfa-fresh-2", "password": "x"}
    )
    assert response.status_code == 200, response.data
    assert response.data["secret"]
    assert response.data["confirmed"] is False


@pytest.mark.django_db
def test_mfa_setup_password_path_rejects_bad_credentials(django_user_model):
    from apps.accounts.views import mfa_setup

    _staff(django_user_model, "mfa-fresh-3", "support")
    response = _mfa_call(
        mfa_setup, {"username": "mfa-fresh-3", "password": "WRONG"}
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_mfa_setup_password_path_closed_once_confirmed(django_user_model):
    """A CONFIRMED account must never re-issue a secret on password alone."""
    from apps.accounts.views import mfa_setup

    user = _staff(django_user_model, "mfa-confirmed", "support")
    row = mfa.provision(user)
    mfa.confirm(user, __import__("pyotp").TOTP(row.secret).now())

    response = _mfa_call(
        mfa_setup, {"username": "mfa-confirmed", "password": "x"}
    )
    assert response.status_code == 403
    assert "confirmed" in response.data["error"]


@pytest.mark.django_db
def test_mfa_setup_password_path_rejects_plain_users(django_user_model):
    from apps.accounts.views import mfa_setup

    django_user_model.objects.create_user(username="mfa-plain", password="x")
    response = _mfa_call(mfa_setup, {"username": "mfa-plain", "password": "x"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# D2 — assignable malformed org_id
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_assignable_bad_org_id_is_400_not_500(django_user_model):
    support = _staff(django_user_model, "asg-support", "support")
    _world(django_user_model)
    request = APIRequestFactory().get("/x?org_id=2")  # "2" is not a UUID
    force_authenticate(request, user=support)
    response = install_assignable(request)
    assert response.status_code == 400
    assert "UUID" in response.data["error"]


# ---------------------------------------------------------------------------
# D3 — the generic command surface must reject ROTATE_CREDENTIAL
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_issue_command_rejects_rotate_credential(django_user_model):
    from apps.accounts.models import OrgMember
    from apps.devices.models import GatewaySlot
    from tests.test_command_issue_path import _call, _manager, _org, _slot

    org = _org()
    slot = _slot(org)
    manager = _manager(org)

    response = _call(manager, issue_gateway_command, "POST",
                     f"/gateways/{slot.slot_id}/commands/",
                     {"kind": "ROTATE_CREDENTIAL"}, gateway_slot_id=slot.slot_id)
    assert response.status_code == 400
    assert "rotate-credential" in response.data["error"]
    assert not GatewayCommand.objects.filter(gateway_slot_id=slot.slot_id).exists()


# ---------------------------------------------------------------------------
# D4 — the slot-FAILED broadcast is scoped and honest
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_reported_failed_only_fails_provision_commands(django_user_model):
    from tests.test_command_issue_path import _org, _slot

    org = _org()
    slot = _slot(org)  # real gateway_slots row — gateway_commands FK requires it
    provision = GatewayCommand.objects.create(
        gateway_slot_id=slot.slot_id, org_id=org.org_id,
        kind=GatewayCommand.KIND_PROVISION, state="SENT",
    )
    reboot = GatewayCommand.objects.create(
        gateway_slot_id=slot.slot_id, org_id=org.org_id,
        kind=GatewayCommand.KIND_REBOOT, state="ACKNOWLEDGED",
    )

    outcome = reconcile_reported(
        {"slot_id": slot.slot_id, "config_version": 0, "state": "FAILED",
         "error": "config rejected"},
        topic_slot_id=slot.slot_id,
    )
    assert outcome == "FAILED"
    provision.refresh_from_db()
    reboot.refresh_from_db()
    assert provision.state == "FAILED"
    assert reboot.state == "ACKNOWLEDGED"  # untouched
    assert reboot.applied_at is None       # no fake applied stamp


# ---------------------------------------------------------------------------
# D4b — the expiry sweep never expires a row that already applied
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_expiry_sweep_never_expires_an_applied_row(django_user_model):
    from apps.provisioning.tasks import sweep_command_expiry
    from tests.test_command_issue_path import _org, _slot

    org = _org()
    slot = _slot(org)
    applied = GatewayCommand.objects.create(
        gateway_slot_id=slot.slot_id, org_id=org.org_id,
        kind=GatewayCommand.KIND_REBOOT, state="APPLIED",
        applied_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
        expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1),
    )
    sweep_command_expiry(limit=50)
    applied.refresh_from_db()
    assert applied.state == "APPLIED"


# ---------------------------------------------------------------------------
# D6 — flat stream metrics must evaluate (the live shape, not the test shape)
#
# Uses the REAL redis the integration tests use (INTEGRATION_REDIS_URL, db 11)
# rather than a hand-rolled fake: the earlier version of this test failed on
# the fake's missing methods, not on the behaviour under test.
# ---------------------------------------------------------------------------

def _integration_redis():
    """Real redis on the integration DB, or None when the suite has no broker."""
    import redis as redis_lib

    from tests.conftest import INTEGRATION_REDIS_URL

    try:
        client = redis_lib.Redis.from_url(INTEGRATION_REDIS_URL, decode_responses=True)
        client.ping()
    except Exception:  # noqa: BLE001 — no broker available in this environment
        return None
    client.flushdb()
    return client


@pytest.mark.django_db
def test_evaluate_entry_fires_on_flat_metrics():
    """The subscriber's live shape is FLAT ({"temp": 40.5}).

    Before D6 the evaluator required {"temp": {"avg": …}} and skipped flat
    values, so threshold alerting never fired for any real reading. A bare
    number must be treated as its own window average.
    """
    import time as _time

    from apps.alerts.models import AlertInstance
    from apps.alerts.services import redis_keys
    from apps.alerts.services.evaluation import evaluate_entry
    from apps.farms.models import Farm, Saloon
    from apps.tenants.models import Organization

    r = _integration_redis()
    if r is None:
        pytest.skip("no redis available for the evaluation test")

    from apps.geography.models import City, Province

    province = Province.objects.create(name="P-d6")
    city = City.objects.create(province=province, name="C-d6")
    org = Organization.objects.create(name="d6-org")
    farm = Farm.objects.create(
        org=org, name="d6-farm", province=province, city=city,
    )
    saloon = Saloon.objects.create(org=org, farm=farm, name="d6-saloon")

    # arm the threshold hash exactly as the recache would
    r.hset(redis_keys.thresholds_hash(saloon.pk), mapping={
        "temp_max": "26.00", "temp_min": "15.00",
        "operational_state": "ACTIVE_FLOCK", "flock_age_days": "0",
    })

    now = _time.time()
    args = (r, saloon.pk, str(org.org_id), farm.pk, "SS-D6", {"temp": 40.5})

    # window 1 arms the breach episode; the 900 s duration gate is not met yet
    assert evaluate_entry(*args, device_ts="2026-10-24T09:00:00Z", clock_now=now) is None

    # window 2 is past the gate — this is what used to silently return None
    fired = evaluate_entry(
        *args, device_ts="2026-10-24T09:16:00Z", clock_now=now + 901
    )
    assert fired is not None, "flat metrics did not evaluate — D6 regressed"
    assert fired.get("metric") == "temp"

    assert AlertInstance.objects.filter(saloon_id=saloon.pk, metric_type="temp").exists()

    r.flushdb()


# ---------------------------------------------------------------------------
# D7 — the hourly aggregate writer
#
# The hypertables are UNMANAGED and `DisableMigrations` never creates them, so
# this follows the project's own DDL-guard pattern (test_followthrough_core):
# create from the migration's SQL when absent, then exercise the builder.
# ---------------------------------------------------------------------------

@pytest.fixture
def telemetry_tables():
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("unmanaged hypertables are Postgres-only")

    from tests.conftest import BACKEND_DIR, _execute_sql, _sql_from_migration

    with connection.cursor() as cursor:
        if cursor.execute(
            "SELECT to_regclass('public.saloon_telemetry') IS NULL"
        ).fetchone()[0]:
            _execute_sql(
                cursor,
                _sql_from_migration(
                    BACKEND_DIR / "apps" / "telemetry" / "migrations"
                    / "0002_timescale_hypertables.py"
                ),
            )
    yield


@pytest.mark.django_db
def test_build_metric_hourly_writes_and_reheals(telemetry_tables):
    from django.db import connection

    from apps.farms.models import Farm, Saloon
    from apps.monitoring.services_aggregates import build_metric_hourly
    from apps.tenants.models import Organization

    from apps.geography.models import City, Province

    province = Province.objects.create(name="P-d7")
    city = City.objects.create(province=province, name="C-d7")
    org = Organization.objects.create(name="d7-org")
    farm = Farm.objects.create(
        org=org, name="d7-farm", province=province, city=city,
    )
    saloon = Saloon.objects.create(org=org, farm=farm, name="d7-saloon")

    # saloon_telemetry.slot_id is an FK to saloon_sensor_slots: telemetry can
    # only exist for a real sensor slot (that is the point of the mapping).
    from apps.devices.models import SaloonPosition, SaloonSensorSlot

    position = SaloonPosition.objects.create(
        saloon=saloon, org=org, label="north",
    )
    SaloonSensorSlot.objects.create(
        slot_id="SS-D7", saloon=saloon, org=org, position=position,
        allowed_metric_keys=["temp"],
    )

    now = dt.datetime.now(dt.timezone.utc)
    bucket = now.replace(minute=0, second=0, microsecond=0)
    device_ts = bucket + dt.timedelta(minutes=17)

    # server_received_ts is part of the dedup key: space the rows explicitly
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO saloon_telemetry
                (server_received_ts, device_ts, org_id, saloon_id, slot_id, metrics)
            VALUES (%s, %s, %s, %s, 'SS-D7', %s),
                   (%s, %s, %s, %s, 'SS-D7', %s)
            """,
            [
                now, device_ts, str(org.org_id), saloon.pk, '{"temp": 20.0}',
                now + dt.timedelta(minutes=3), device_ts + dt.timedelta(minutes=3),
                str(org.org_id), saloon.pk, '{"temp": 30.0}',
            ],
        )

    written = build_metric_hourly(hours=26)
    assert written >= 1, "the hourly writer produced nothing — D7 regressed"

    with connection.cursor() as cur:
        cur.execute(
            "SELECT avg_val, min_val, max_val, sample_count "
            "FROM metric_hourly_aggregates WHERE slot_id='SS-D7' AND metric_key='temp'"
        )
        row = cur.fetchone()
    assert row is not None
    assert float(row[0]) == pytest.approx(25.0)
    assert float(row[1]) == pytest.approx(20.0)
    assert float(row[2]) == pytest.approx(30.0)
    assert row[3] == 2

    # idempotent: a re-run corrects instead of duplicating
    build_metric_hourly(hours=26)
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM metric_hourly_aggregates "
            "WHERE slot_id='SS-D7' AND metric_key='temp'"
        )
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# D14 — retroactive replay analysis must see the live (flat) metric shape
# ---------------------------------------------------------------------------

def test_window_breaches_reads_flat_metrics():
    """A buffered window that breached must produce a REPLAY_ANALYSIS row.

    The subscriber stores flat metrics ({"temp": 40.0}); _window_breaches used
    to require {"temp": {"avg": …}} and silently found no breach — the same
    defect D6 fixed in the realtime evaluator.
    """
    from types import SimpleNamespace

    from apps.alerts.services.replay import _window_breaches

    thresholds = {"temp_max": "26.00", "nh3_max": "25.00"}

    flat = [SimpleNamespace(metrics={"temp": 40.0, "nh3": 9.0})]
    assert _window_breaches(flat, thresholds) == {"temp": 40.0}

    # the window shape must keep working too
    window = [SimpleNamespace(metrics={"temp": {"avg": 30.0, "min": 29.0, "max": 31.0}})]
    assert _window_breaches(window, thresholds) == {"temp": 30.0}

    # in-band readings produce nothing, and a non-numeric value is skipped
    assert _window_breaches([SimpleNamespace(metrics={"temp": 24.0})], thresholds) == {}
    assert _window_breaches(
        [SimpleNamespace(metrics={"temp": "NOT_A_NUMBER"})], thresholds
    ) == {}

    # worst value across the window wins
    rows = [
        SimpleNamespace(metrics={"temp": 30.0}),
        SimpleNamespace(metrics={"temp": 41.5}),
        SimpleNamespace(metrics={"temp": 35.0}),
    ]
    assert _window_breaches(rows, thresholds) == {"temp": 41.5}


# ---------------------------------------------------------------------------
# D15 — a deleted rule must drop its band from the cached hash
# ---------------------------------------------------------------------------

def test_recache_drops_a_stale_band(django_user_model):
    """Deleting a rule must remove its {metric}_max, not leave it armed."""
    from apps.alerts.services import redis_keys
    from apps.alerts.services.thresholds import recache_saloon
    from tests.conftest import INTEGRATION_REDIS_URL

    import redis as redis_lib

    try:
        client = redis_lib.Redis.from_url(INTEGRATION_REDIS_URL, decode_responses=True)
        client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("no redis available for the recache test")

    key = redis_keys.thresholds_hash(999)
    client.delete(key)
    client.hset(key, mapping={"temp_max": "26.00", "thi_max": "80.00"})

    # the recache for a saloon with no rules must clear every stale band
    from apps.alerts.services import thresholds as th

    original = th.build_hash_for_saloon
    try:
        th.build_hash_for_saloon = lambda saloon, clock=None: {"operational_state": "ACTIVE_FLOCK"}
        from apps.farms.models import Saloon
        from apps.tenants.models import Organization
        from apps.geography.models import City, Province

        p = Province.objects.create(name="P-d15")
        c = City.objects.create(province=p, name="C-d15")
        org = Organization.objects.create(name="d15-org")
        from apps.farms.models import Farm

        f = Farm.objects.create(org=org, name="d15-farm", province=p, city=c)
        s = Saloon.objects.create(org=org, farm=f, name="d15-saloon")
        client.hset(redis_keys.thresholds_hash(s.pk), mapping={
            "temp_max": "26.00", "thi_max": "80.00",
        })
        recache_saloon(s.pk)
        left = client.hgetall(redis_keys.thresholds_hash(s.pk))
    finally:
        th.build_hash_for_saloon = original
        client.flushdb()

    assert "thi_max" not in left, f"stale band survived the recache: {left}"
    assert "temp_max" not in left, f"stale band survived the recache: {left}"


# ---------------------------------------------------------------------------
# D17 — one stream entry must be processed ONCE per drain cycle
#
# The live WebSocket drill caught every telemetry.update arriving twice. Cause:
# drain_stream does a '>' read and then a PEL ("0") read in the SAME cycle, and
# the '>' read has already put those entries in our own PEL — so both reads
# return the same entry, and both copies were appended to raw_entries. Rows
# looked right only because bulk_create(ignore_conflicts=True) hid it.
#
# Uses REAL redis (the PEL behaviour is the whole point — no fake models it)
# and asserts the two things the bug corrupted: the push count and `processed`.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_drain_processes_each_entry_once_per_cycle(django_user_model):
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Farm, Saloon
    from apps.geography.models import City, Province
    from apps.telemetry import consumers
    from apps.tenants.models import Organization
    from tests.conftest import INTEGRATION_REDIS_URL

    import redis as redis_lib

    try:
        client = redis_lib.Redis.from_url(INTEGRATION_REDIS_URL, decode_responses=True)
        client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("no redis available for the drain test")

    province = Province.objects.create(name="P-d17")
    city = City.objects.create(province=province, name="C-d17")
    org = Organization.objects.create(name="d17-org")
    farm = Farm.objects.create(org=org, name="d17-farm", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="d17-saloon")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    SaloonSensorSlot.objects.create(
        slot_id="SS-D17", saloon=saloon, org=org, position=position,
        allowed_metric_keys=["temp"],
    )

    stream = "test:d17:realtime"
    client.delete(stream)
    consumers._redis_client = client  # drain uses this client

    pushes = []

    def _record_push(org_id, entries, channel_layer=None):
        pushes.extend(entries)
        return len(entries)

    import apps.telemetry.ws as ws_mod

    original_push = ws_mod.push_telemetry_updates
    ws_mod.push_telemetry_updates = _record_push
    try:
        now = dt.datetime.now(dt.timezone.utc)
        client.xadd(stream, {
            "org_id": str(org.org_id),
            "farm_id": str(farm.pk),
            "gateway_slot_id": "GW-D17",
            "slot_id": "SS-D17",
            "device_ts": now.isoformat(),
            "recv_ts": str(now.timestamp()),
            "is_buffered": "False",
            "clock_drift": "False",
            "metrics": '{"temp": 25.0}',
        })
        processed = consumers.drain_stream(stream, count=10)
    finally:
        ws_mod.push_telemetry_updates = original_push
        client.delete(stream)
        client.flushdb()
        consumers._redis_client = None

    assert processed == 1, f"one entry processed {processed} times (D17 regressed)"
    assert len(pushes) == 1, f"one entry pushed {len(pushes)} times to the dashboard (D17 regressed)"
