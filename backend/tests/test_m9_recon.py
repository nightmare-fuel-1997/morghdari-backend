"""v0.9 M9-10/B5 acceptance — daily reconciliation ledger.

Service-level with a stubbed Redis stream (hermetic — no broker needed):
delivery math, reason/gateway attribution, day attribution by payload
device_ts (device truth) with entry-id fallback, alert listing, and the
fail-soft DB-half degradation. Plus the endpoint gates (engineer+ reads,
workers 403, bad day 400) and the management command.
"""

import datetime as dt
import json
import uuid

import pytest

DAY = dt.date(2026, 9, 10)
TEHRAN = dt.timezone(dt.timedelta(hours=3, minutes=30))


class StubRedis:
    def __init__(self, entries=None, down=False):
        self._entries = entries or []
        self._down = down

    def xrevrange(self, *args, **kwargs):
        if self._down:
            raise ConnectionError("redis down")
        return self._entries


def _dlq(topic, reason, device_ts=None, entry_ms=None):
    payload = json.dumps({"device_ts": device_ts} if device_ts else {"ping": 1})
    ms = entry_ms if entry_ms is not None else int(
        dt.datetime(2026, 9, 10, 6, 0, tzinfo=dt.timezone.utc).timestamp() * 1000
    )
    return (f"{ms}-0", {"topic": topic, "payload": payload, "reason": reason})


@pytest.fixture
def recon_tables(db):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS saloon_telemetry ("
            "server_received_ts TIMESTAMP NOT NULL PRIMARY KEY, device_ts TIMESTAMP NOT NULL, "
            "org_id VARCHAR(32) NOT NULL, saloon_id INTEGER NOT NULL, slot_id VARCHAR(50) NOT NULL, "
            "is_buffered BOOLEAN, clock_drift BOOLEAN, metrics TEXT NOT NULL)"
        )
        cursor.execute("DELETE FROM saloon_telemetry")
    yield


@pytest.fixture
def recon_world(db, recon_tables):
    from django.contrib.auth import get_user_model

    from apps.accounts.models import OrgMember
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Farm, Saloon
    from apps.geography.models import City, Province
    from apps.telemetry.models import SaloonTelemetry
    from apps.tenants.models import Organization

    tag = "rc" + uuid.uuid4().hex[:8]
    org = Organization.objects.create(name=f"org-{tag}")
    users = {}
    for username, role in (("owner", "owner"), ("manager", "manager"),
                           ("engineer", "engineer"), ("worker", "worker")):
        user = get_user_model().objects.create_user(username=f"{username}-{tag}", password="x")
        OrgMember.objects.create(user=user, org=org, role=role, is_active=True)
        users[role] = user
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"r-{tag}", saloon=saloon, org=org, position=position
    )
    base = dt.datetime(2026, 9, 10, 6, 0, tzinfo=dt.timezone.utc)
    for minute in range(3):
        ts = base + dt.timedelta(minutes=minute)
        SaloonTelemetry.objects.create(
            server_received_ts=ts, device_ts=ts, org=org, saloon=saloon,
            slot=slot, is_buffered=False, metrics={"temp": 25.0},
        )
    return locals()


def _topic(org_id):
    return f"{org_id}/farms/1/gateways/GW-RC/telemetry"


@pytest.mark.django_db(transaction=False)
def test_accounted_math_and_attribution(recon_world):
    from apps.monitoring.ops_services import recon_day_report

    w = recon_world
    org_id = str(w["org"].org_id)
    rc = StubRedis([
        _dlq(_topic(org_id), "UNKNOWN_SLOT", "2026-09-10T06:05:00Z"),
        _dlq(_topic(org_id), "UNKNOWN_SLOT", "2026-09-09T06:05:00Z"),  # other day
    ])
    report = recon_day_report(DAY, redis_client=rc)
    assert report["day"] == "2026-09-10"
    assert len(report["orgs"]) == 1
    row = report["orgs"][0]
    assert (row["db_rows"], row["dlq_rows"], row["accounted_pct"]) == (3, 1, 75.0)
    assert row["by_reason"] == {"UNKNOWN_SLOT": 1}
    assert row["by_gateway"] == {"GW-RC": 1}
    assert row["dlq_unavailable"] is False


@pytest.mark.django_db(transaction=False)
def test_alerts_listed_and_redis_down_degrades(recon_world):
    import datetime as _dt

    from apps.alerts.models import AlertInstance
    from apps.monitoring.ops_services import recon_day_report

    w = recon_world
    base = _dt.datetime(2026, 9, 10, 7, 0, tzinfo=_dt.timezone.utc)
    AlertInstance.objects.create(
        org=w["org"], farm=w["farm"], saloon=w["saloon"], metric_type="temp",
        current_status="ACTIVE_TIER_1", triggered_at=base, highest_tier_reached=1,
    )
    full = recon_day_report(DAY, redis_client=StubRedis())
    assert len(full["orgs"][0]["alerts"]) == 1
    assert full["orgs"][0]["alerts"][0]["metric"] == "temp"
    degraded = recon_day_report(DAY, redis_client=StubRedis(down=True))
    assert degraded["orgs"][0]["dlq_unavailable"] is True
    assert degraded["orgs"][0]["db_rows"] == 3


def _call(view, method, path, user, roles, org, query=None):
    from rest_framework.test import APIRequestFactory, force_authenticate

    request = getattr(APIRequestFactory(), method.lower())(path, data=query or {}, format="json")
    force_authenticate(request, user=user)
    request.jwt_claims = {"org_id": str(org.org_id), "roles": roles}
    return view(request)


@pytest.mark.django_db(transaction=False)
def test_endpoint_gates_and_shape(recon_world, monkeypatch):
    import apps.monitoring.ops_views as views
    from apps.monitoring.ops_views import reconciliation

    w = recon_world
    monkeypatch.setattr(views.svc, "_redis_client", lambda: StubRedis())
    ok = _call(reconciliation, "GET", "/api/v1/reconciliation/", w["users"]["engineer"],
               ["engineer"], w["org"], {"day": "2026-09-10"})
    assert ok.status_code == 200, ok.data
    assert ok.data["day"] == "2026-09-10"
    assert ok.data["orgs"][0]["db_rows"] == 3
    denied = _call(reconciliation, "GET", "/api/v1/reconciliation/", w["users"]["worker"],
                   ["worker"], w["org"], {"day": "2026-09-10"})
    assert denied.status_code == 403
    bad = _call(reconciliation, "GET", "/api/v1/reconciliation/", w["users"]["engineer"],
                ["engineer"], w["org"], {"day": "not-a-day"})
    assert bad.status_code == 400


@pytest.mark.django_db(transaction=False)
def test_command_prints_ledger(recon_world, monkeypatch, capsys):
    import apps.monitoring.ops_services as services
    from django.core.management import call_command

    monkeypatch.setattr(services, "_redis_client", lambda: StubRedis())
    call_command("recon_day", "--day", "2026-09-10")
    out = capsys.readouterr().out
    assert "db=3 dlq=0" in out and "accounted=100.0%" in out
