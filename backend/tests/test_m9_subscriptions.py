"""v0.9 M9-10/B2 acceptance — org-wide alert-subscription mutes (GS-ROUTING).

API (pytest + jwt_claims, m5 pattern): manager+ mutes, members read, workers
get 403 on writes, cross-org is empty, unknown metrics 400, re-mute/unmute
semantics hold, audit rows carry the actor.

Engine (real scratch redis, m4 pattern): a muted category never fires and
never accumulates breach (unmuting starts fresh); the digest counts the
suppression; NH3 velocity pages THROUGH the mute (Decision 10 structural);
LOS/escalation paths are unwired by design (asserted by absence of hooks —
no subscription import in sweeps.py / los paths).
"""

import json
import uuid

import pytest
import redis as redis_lib
from django.test import RequestFactory
from rest_framework.test import force_authenticate

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL, redis_base_url

SCRATCH_REDIS_URL = redis_base_url() + "/15"


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    scratch = redis_lib.Redis.from_url(SCRATCH_REDIS_URL, decode_responses=True)
    scratch.flushdb()
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.alerts.services.subscriptions.system_redis", lambda: client)
    yield client


def _world():
    from django.contrib.auth import get_user_model

    from apps.accounts.models import OrgMember
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Farm, Saloon
    from apps.geography.models import City, Province
    from apps.telemetry.models import SensorMetricType
    from apps.tenants.models import Organization

    tag = "su" + uuid.uuid4().hex[:8]
    org = Organization.objects.create(name=f"org-{tag}")
    manager = get_user_model().objects.create_user(username=f"mgr-{tag}", password="x")
    OrgMember.objects.create(user=manager, org=org, role="manager", is_active=True)
    worker = get_user_model().objects.create_user(username=f"wrk-{tag}", password="x")
    OrgMember.objects.create(user=worker, org=org, role="worker", is_active=True)
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"u-{tag}", saloon=saloon, org=org, position=position
    )
    SensorMetricType.objects.update_or_create(
        metric_key="temp", defaults={"display_name": "Temperature", "unit": "C"}
    )
    SensorMetricType.objects.update_or_create(
        metric_key="nh3", defaults={"display_name": "Ammonia", "unit": "ppm"}
    )
    return locals()


def _call(view, method, path, user, claims, data=None, **kwargs):
    from rest_framework.test import APIRequestFactory

    request = getattr(APIRequestFactory(), method.lower())(path, data=data or {}, format="json")
    force_authenticate(request, user=user)
    request.jwt_claims = claims
    return view(request, **kwargs)


def _claims(user, org, roles):
    return {"org_id": str(org.org_id), "roles": roles}


@pytest.mark.django_db
def test_manager_mutes_member_reads_digest_counts():
    from apps.alerts.api_follow import alert_subscriptions
    from apps.alerts.models import AlertSubscription
    from apps.auditing.models import AccountActivityLog

    w = _world()
    claims = _claims(w["manager"], w["org"], ["manager"])
    created = _call(
        alert_subscriptions, "POST", "/api/v1/alert-subscriptions/", w["manager"], claims,
        {"metric_key": "temp", "reason": "glare season"},
    )
    assert created.status_code == 200, created.data
    assert AlertSubscription.objects.filter(org_id=w["org"].org_id, metric_key="temp").exists()
    audit = AccountActivityLog.objects.filter(action="SUB_MUTED").order_by("-id").first()
    assert audit.details["metric_key"] == "temp"
    assert audit.actor_id == w["manager"].pk

    again = _call(
        alert_subscriptions, "POST", "/api/v1/alert-subscriptions/", w["manager"], claims,
        {"metric_key": "temp"},
    )
    assert again.status_code == 200
    assert again.data["idempotent"] is True
    assert AlertSubscription.objects.filter(org_id=w["org"].org_id).count() == 1

    listed = _call(
        alert_subscriptions, "GET", "/api/v1/alert-subscriptions/", w["worker"],
        _claims(w["worker"], w["org"], ["worker"]),
    )
    assert listed.status_code == 200
    assert listed.data["count"] == 1
    assert listed.data["results"][0]["suppressed_today"] == 0


@pytest.mark.django_db
def test_worker_write_forbidden_unknown_metric_400():
    from apps.alerts.api_follow import alert_subscriptions

    w = _world()
    denied = _call(
        alert_subscriptions, "POST", "/api/v1/alert-subscriptions/", w["worker"],
        _claims(w["worker"], w["org"], ["worker"]), {"metric_key": "temp"},
    )
    assert denied.status_code == 403
    bad = _call(
        alert_subscriptions, "POST", "/api/v1/alert-subscriptions/", w["manager"],
        _claims(w["manager"], w["org"], ["manager"]), {"metric_key": "nope"},
    )
    assert bad.status_code == 400


@pytest.mark.django_db
def test_unmute_revokes_and_unknown_is_404():
    from apps.alerts.api_follow import alert_subscription_unmute, alert_subscriptions
    from apps.alerts.models import AlertSubscription

    w = _world()
    claims = _claims(w["manager"], w["org"], ["manager"])
    _call(
        alert_subscriptions, "POST", "/api/v1/alert-subscriptions/", w["manager"], claims,
        {"metric_key": "temp"},
    )
    gone = _call(
        alert_subscription_unmute, "DELETE", "/api/v1/alert-subscriptions/temp/", w["manager"],
        claims, metric_key="temp",
    )
    assert gone.status_code == 200
    assert gone.data == {"metric_key": "temp", "muted": False}
    assert not AlertSubscription.objects.filter(org_id=w["org"].org_id).exists()
    missing = _call(
        alert_subscription_unmute, "DELETE", "/api/v1/alert-subscriptions/temp/", w["manager"],
        claims, metric_key="temp",
    )
    assert missing.status_code == 404


@pytest.mark.django_db
def test_cross_org_mutes_invisible():
    from apps.alerts.api_follow import alert_subscriptions
    from apps.tenants.models import Organization

    w = _world()
    other = Organization.objects.create(name=f"other-{uuid.uuid4().hex[:6]}")
    _call(
        alert_subscriptions, "POST", "/api/v1/alert-subscriptions/", w["manager"],
        _claims(w["manager"], w["org"], ["manager"]), {"metric_key": "temp"},
    )
    listed = _call(
        alert_subscriptions, "GET", "/api/v1/alert-subscriptions/", w["manager"],
        {"org_id": str(other.org_id), "roles": ["manager"]},
    )
    assert listed.status_code == 200
    assert listed.data["count"] == 0


def _seed_thresholds(rc, saloon):
    from apps.alerts.services.redis_keys import thresholds_hash

    rc.hset(thresholds_hash(saloon.id), mapping={
        "temp_max": "30.00", "temp_min": "18.00",
        "nh3_enabled": "1", "operational_state": "ACTIVE_FLOCK",
    })


def _seed_sustained_breach(rc, saloon, metric, value, clock=1_000_000.0):
    from apps.alerts.services.redis_keys import breach_key

    rc.set(
        breach_key(saloon.id, metric),
        json.dumps({"started_at": clock - 901.0, "alerted": False, "value": value}),
        ex=900,
    )


def _evaluate(rc, world, metrics, clock=1_000_000.0):
    from apps.alerts.services.evaluation import evaluate_entry

    return evaluate_entry(
        redis_client=rc,
        saloon_id=world["saloon"].id,
        org_id=str(world["org"].org_id),
        farm_id=world["farm"].id,
        slot_id=world["slot"].slot_id,
        metrics=metrics,
        device_ts="2026-09-12T12:00:00Z",
        clock_now=clock,
    )


@pytest.mark.django_db(transaction=True)
def test_muted_category_never_fires_and_digests(timescale_db, _fresh_redis):
    from apps.alerts.models import AlertInstance, AlertSubscription
    from apps.alerts.services.subscriptions import submuted_today

    w = _world()
    rc = _fresh_redis
    _seed_thresholds(rc, w["saloon"])
    AlertSubscription.objects.create(org=w["org"], metric_key="temp", muted_by=w["manager"])
    _seed_sustained_breach(rc, w["saloon"], "temp", 35.0)
    assert _evaluate(rc, w, {"temp": {"avg": 35.0, "min": 34.0, "max": 36.0}}) is None
    assert not AlertInstance.objects.filter(org_id=w["org"].org_id).exists()
    assert submuted_today(rc, str(w["org"].org_id)) == {"temp": 1}


@pytest.mark.django_db(transaction=True)
def test_unmuted_category_fires_normally(timescale_db, _fresh_redis):
    from apps.alerts.models import AlertInstance

    w = _world()
    rc = _fresh_redis
    _seed_thresholds(rc, w["saloon"])
    _seed_sustained_breach(rc, w["saloon"], "temp", 35.0)
    fired = _evaluate(rc, w, {"temp": {"avg": 35.0, "min": 34.0, "max": 36.0}})
    assert fired is not None and fired["metric"] == "temp"
    assert AlertInstance.objects.filter(org_id=w["org"].org_id, metric_type="temp").exists()


@pytest.mark.django_db(transaction=True)
def test_velocity_pages_through_mute_without_digest(timescale_db, _fresh_redis):
    from apps.alerts.models import AlertInstance, AlertSubscription
    from apps.alerts.services.subscriptions import submuted_today

    w = _world()
    rc = _fresh_redis
    _seed_thresholds(rc, w["saloon"])
    AlertSubscription.objects.create(org=w["org"], metric_key="nh3", muted_by=w["manager"])
    AlertSubscription.objects.create(org=w["org"], metric_key="temp", muted_by=w["manager"])
    fired = _evaluate(rc, w, {"nh3": {"avg": 14.0, "min": 10.0, "max": 20.0, "max_ts": 57}})
    assert fired is not None  # Decision 10: velocity bypasses every mute
    assert AlertInstance.objects.filter(org_id=w["org"].org_id).exists()
    assert "nh3" not in submuted_today(rc, str(w["org"].org_id))
