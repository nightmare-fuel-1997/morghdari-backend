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

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# D1 — the first-run MFA path
# ---------------------------------------------------------------------------

def _mfa_call(view, data, user=None):
    request = APIRequestFactory().post("/x", data=data, format="json")
    if user is not None:
        force_authenticate(request, user=user)
    return view(request)


def test_mfa_setup_password_path_issues_a_secret(django_user_model):
    from apps.accounts.views import mfa_setup

    _staff(django_user_model, "mfa-fresh-2", "support")
    response = _mfa_call(
        mfa_setup, {"username": "mfa-fresh-2", "password": "x"}
    )
    assert response.status_code == 200, response.data
    assert response.data["secret"]
    assert response.data["confirmed"] is False


def test_mfa_setup_password_path_rejects_bad_credentials(django_user_model):
    from apps.accounts.views import mfa_setup

    _staff(django_user_model, "mfa-fresh-3", "support")
    response = _mfa_call(
        mfa_setup, {"username": "mfa-fresh-3", "password": "WRONG"}
    )
    assert response.status_code == 401


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


def test_mfa_setup_password_path_rejects_plain_users(django_user_model):
    from apps.accounts.views import mfa_setup

    django_user_model.objects.create_user(username="mfa-plain", password="x")
    response = _mfa_call(mfa_setup, {"username": "mfa-plain", "password": "x"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# D2 — assignable malformed org_id
# ---------------------------------------------------------------------------

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

def test_reported_failed_only_fails_provision_commands(django_user_model):
    org = _world(django_user_model)["org"]
    provision = GatewayCommand.objects.create(
        gateway_slot_id="GW-FAIL-SCOPE", org_id=org.org_id,
        kind=GatewayCommand.KIND_PROVISION, state="SENT",
    )
    reboot = GatewayCommand.objects.create(
        gateway_slot_id="GW-FAIL-SCOPE", org_id=org.org_id,
        kind=GatewayCommand.KIND_REBOOT, state="ACKNOWLEDGED",
    )

    outcome = reconcile_reported(
        {"slot_id": "GW-FAIL-SCOPE", "config_version": 0, "state": "FAILED",
         "error": "config rejected"},
        topic_slot_id="GW-FAIL-SCOPE",
    )
    assert outcome == "FAILED"
    provision.refresh_from_db()
    reboot.refresh_from_db()
    assert provision.state == "FAILED"
    assert reboot.state == "ACKNOWLEDGED"  # untouched
    assert reboot.applied_at is None       # no fake applied stamp


def test_expiry_sweep_never_expires_an_applied_row(django_user_model):
    from apps.provisioning.tasks import sweep_command_expiry

    org = _world(django_user_model)["org"]
    applied = GatewayCommand.objects.create(
        gateway_slot_id="GW-EXP-1", org_id=org.org_id,
        kind=GatewayCommand.KIND_REBOOT, state="APPLIED",
        applied_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
        expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1),
    )
    sweep_command_expiry(limit=50)
    applied.refresh_from_db()
    assert applied.state == "APPLIED"


# ---------------------------------------------------------------------------
# D6 — flat stream metrics must evaluate
# ---------------------------------------------------------------------------

def test_evaluate_entry_fires_on_flat_metrics(rf=None, django_user_model=None, settings=None):
    """The subscriber's live shape is flat; a sustained breach must still arm."""
    import time as _time

    from django.core.cache import cache

    from apps.alerts.services import redis_keys

    class FakeRedis(dict):
        def hgetall(self, key):
            return dict(self.get(key, {}))

        def hset(self, key, mapping=None, **kw):
            self.setdefault(key, {}).update(mapping or {})

        def expire(self, key, ttl):
            pass

        def pipeline(self):
            pipe = _FakePipeline(self)
            return pipe

        def set(self, key, value, ex=None):
            self[key] = value

        def get(self, key):
            return self.get(key)

    class _FakePipeline:
        def __init__(self, store):
            self.store = store
            self.ops = []

        def hset(self, key, mapping=None, **kw):
            self.ops.append(("hset", key, mapping or {}))

        def expire(self, key, ttl):
            self.ops.append(("expire", key, ttl))

        def setex(self, key, ttl, value):
            self.ops.append(("setex", key, ttl, value))

        def execute(self):
            for op in self.ops:
                if op[0] == "hset":
                    self.store.setdefault(op[1], {}).update(op[2])
                elif op[0] == "setex":
                    self.store[op[1]] = op[3]
            return []

    r = FakeRedis()
    r[redis_keys.thresholds_hash(77)] = {"temp_max": "26.00", "temp_min": "15.00"}

    now = _time.time()
    # window 1 — arms the breach accumulator
    out1 = evaluate_entry(
        r, 77, "org-x", 1, "SS-X", {"temp": 40.5},
        device_ts="2026-10-24T09:00:00Z", clock_now=now,
    )
    assert out1 is None  # duration gate not yet satisfied (900 s default)
    # window 2 — past the gate
    out2 = evaluate_entry(
        r, 77, "org-x", 1, "SS-X", {"temp": 40.5},
        device_ts="2026-10-24T09:16:00Z", clock_now=now + 901,
    )
    assert out2 is not None
    assert out2.get("metric") == "temp"


# ---------------------------------------------------------------------------
# D7 — the hourly aggregate writer exists and is idempotent
# ---------------------------------------------------------------------------

def test_build_metric_hourly_writes_and_reheals(django_user_model):
    import uuid as _uuid

    from django.db import connection

    from apps.monitoring.services_aggregates import build_metric_hourly
    from apps.tenants.models import Organization

    org = Organization.objects.create(name="hourly-org")
    org_id = str(org.org_id)
    now = dt.datetime.now(dt.timezone.utc)
    bucket = now.replace(minute=0, second=0, microsecond=0)
    device_ts = bucket + dt.timedelta(minutes=17)

    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO saloon_telemetry
                (server_received_ts, device_ts, org_id, saloon_id, slot_id, metrics)
            VALUES (now(), %s, %s, 55, 'SS-HOURLY-T', %s), (now(), %s, %s, 55, 'SS-HOURLY-T', %s)
            """,
            [
                device_ts, org_id, '{"temp": 20.0, "humidity": 50.0}',
                device_ts + dt.timedelta(minutes=3), org_id, '{"temp": 30.0, "humidity": 60.0}',
            ],
        )

    written = build_metric_hourly(hours=26)
    assert written >= 2  # temp + humidity at least

    with connection.cursor() as cur:
        cur.execute(
            "SELECT avg_val, sample_count FROM metric_hourly_aggregates "
            "WHERE slot_id='SS-HOURLY-T' AND metric_key='temp'"
        )
        row = cur.fetchone()
    assert row is not None
    assert float(row[0]) == pytest.approx(25.0)
    assert row[1] == 2

    # idempotent: re-running corrects instead of duplicating
    assert build_metric_hourly(hours=26) >= 2
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM metric_hourly_aggregates "
            "WHERE slot_id='SS-HOURLY-T' AND metric_key='temp'"
        )
        assert cur.fetchone()[0] == 1
