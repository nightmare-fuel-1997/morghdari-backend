"""Follow-through CORE acceptance — contract v1.7.2, F1–F15 + the role change.

Gates (docs/api-followthrough-v1.md):

- **The two writers the engine was missing.** F14 sets `flock_cycles.end_date`
  (nothing else could) and the save MUST re-arm the saloon's threshold hash
  through the existing post_save fast path. F15 writes `mortality_records`,
  the table `GET /saloons/{id}/kpis/` computes `mortality_pct` from — the
  end-to-end test asserts the KPI number MOVES after an API-created row, which
  is the failure the audit found (a farm-facing figure that could only read 0).
- **Delete guards.** `409 farm not empty`; a saloon refuses while it has any
  flock history at all (an ACTIVE flock is the first and loudest case; a closed
  one is still RESTRICT-bound, which only Postgres revealed), telemetry history,
  or any still-referencing relation (the ProtectedError branch). Nothing
  cascades.
- **Role + scope fences.** owner for farms, manager+ for saloons/positions/
  mutes, engineer+ for the cycle endpoints and the org feed. `account_role_scopes`
  rows NARROW a write to a 403 — never a silent widen (the v1.7.1 template
  inversion, kept here for the topology writes).
- **Mute rules.** ≤72 h, no overlap per saloon, no fully-past window; the row
  reads `active` exactly when the ENGINE's predicate
  (`services.mutes.active_maintenance_mute`) says so — velocity bypass itself is
  pinned in test_m4_10, not re-implemented here.
- **Compat slugs.** A farm/saloon created through the API is resolvable by the
  Flutter aliases IMMEDIATELY (the slug row is minted in the same transaction).
  Postgres-only: `legacy_slug_map` exists solely as the compat app's RunSQL DDL,
  so that test skips on the sqlite loop and runs in CI (test_m7_17's pattern).

Recache assertions are split like the v1.7.1 module: the MESSAGE (which task a
flock close fires) is captured by patching `.delay`; the RESOLUTION is left to
the alerts suites. The writes under test also fire those tasks for real (eager
Celery), so an autouse fixture swaps the cache client for a hash store — the
module needs no Redis container.

فارسی: پذیرشِ موجِ دامنهٔ «follow-through» — دو نویسندهٔ گم‌شده (بستنِ دوره و ثبتِ
تلفات)، نگهبان‌های حذف، مرزهای نقش و دامنه، قواعد پنجرهٔ بی‌صداکننده و نگاشتِ
شناسه‌های فلاتر در همان تراکنش.
"""

import uuid
from datetime import date, datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone as dj_tz
from rest_framework.test import APIClient

from apps.accounts.models import AccountRoleScope, OrgMember, UserSession
from apps.alerts.models import AlertInstance, MaintenanceMute, MortalityRecord
from apps.alerts.services import thresholds as thresholds_service
from apps.alerts.services.mutes import active_maintenance_mute
from apps.auditing.models import AccountActivityLog
from apps.devices.models import SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province, Village
from apps.telemetry.models import FlockCycle, SaloonTelemetry
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"
TEHRAN = ZoneInfo("Asia/Tehran")

pytestmark = pytest.mark.django_db


class _HashStore:
    """Minimal stand-in for the alerts threshold cache (see test_m6_11)."""

    def __init__(self):
        self.hashes: dict = {}
        self.strings: dict = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(
            {k: str(v) for k, v in (mapping or {}).items()}
        )
        return len(mapping or {})

    def setex(self, key, ttl, value):
        self.strings[key] = (ttl, value)

    def delete(self, *keys):
        return sum(1 for key in keys if self.hashes.pop(key, None) is not None)

    def pipeline(self):
        return self

    def execute(self):
        return []


@pytest.fixture(autouse=True)
def threshold_cache_hash_store(monkeypatch):
    """No live Redis: the writes under test legitimately trigger cache fast paths."""
    monkeypatch.setattr(thresholds_service, "_redis", lambda: _HashStore())


# ---------------------------------------------------------------------------
# World builder
# ---------------------------------------------------------------------------


def _make_world(tag=None):
    tag = tag or uuid.uuid4().hex[:6]
    province = Province.objects.create(name=f"fp-{tag}")
    city = City.objects.create(province=province, name=f"fc-{tag}")
    village = Village.objects.create(city=city, name=f"fv-{tag}")
    far_province = Province.objects.create(name=f"fp2-{tag}")
    far_city = City.objects.create(province=far_province, name=f"fc2-{tag}")
    far_village = Village.objects.create(city=far_city, name=f"fv2-{tag}")

    org = Organization.objects.create(name=f"fo-{tag}")
    other = Organization.objects.create(name=f"fo2-{tag}")

    farm = Farm.objects.create(org=org, name=f"Farm-{tag}", province=province, city=city)
    empty_farm = Farm.objects.create(
        org=org, name=f"Empty-{tag}", province=province, city=city
    )
    bare_farm = Farm.objects.create(
        org=org, name=f"Bare-{tag}", province=province, city=city
    )
    foreign_farm = Farm.objects.create(
        org=other, name=f"Foreign-{tag}", province=far_province, city=far_city
    )
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"Saloon-{tag}")
    foreign_saloon = Saloon.objects.create(
        org=other, farm=foreign_farm, name=f"ForeignSaloon-{tag}"
    )

    started = dj_tz.now() - timedelta(days=3)
    flock = FlockCycle.objects.create(
        org=org,
        saloon=saloon,
        breed_type="ross",
        start_date=started,
        placement_count=100,
    )

    def member(username, role, org_row=org):
        user = User.objects.create_user(username=username, password=PASSWORD)
        return OrgMember.objects.create(user=user, org=org_row, role=role, is_active=True)

    users = {
        role: member(f"ft-{role}-{tag}", role)
        for role in ("owner", "manager", "engineer", "worker", "reviewer")
    }
    foreign_owner = member(f"ft-foreign-{tag}", "owner", org_row=other)
    # A saloon in ANOTHER farm of the same org — the cross-farm probe.
    other_saloon = Saloon.objects.create(org=org, farm=empty_farm, name=f"Saloon2-{tag}")
    other_flock = FlockCycle.objects.create(
        org=org,
        saloon=other_saloon,
        breed_type="ross",
        start_date=started,
        placement_count=50,
    )

    return {
        "tag": tag,
        "org": org,
        "other": other,
        "province": province,
        "city": city,
        "village": village,
        "far_province": far_province,
        "far_city": far_city,
        "far_village": far_village,
        "farm": farm,
        "empty_farm": empty_farm,
        "bare_farm": bare_farm,
        "foreign_farm": foreign_farm,
        "saloon": saloon,
        "other_saloon": other_saloon,
        "foreign_saloon": foreign_saloon,
        "flock": flock,
        "other_flock": other_flock,
        "users": users,
        "foreign_owner": foreign_owner,
    }


def _client_for(member):
    client = APIClient()
    res = client.post(
        "/api/v1/auth/login/",
        {"username": member.user.username, "password": PASSWORD},
        format="json",
    )
    assert res.status_code == 200, res.content
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access_token']}")
    return client


def _as(world, role):
    return _client_for(world["users"][role])


def _audit(action, org_id):
    return AccountActivityLog.objects.filter(action=action, org_id=org_id)


# ---------------------------------------------------------------------------
# F1 — POST /farms/
# ---------------------------------------------------------------------------


def test_create_farm_owner_only_and_geography_validated():
    world = _make_world()
    body = {
        "name": "New Farm",
        "province_id": world["province"].pk,
        "city_id": world["city"].pk,
        "village_id": world["village"].pk,
    }

    res = _as(world, "owner").post("/api/v1/farms/", body, format="json")
    assert res.status_code == 201, res.content
    assert res.data["name"] == "New Farm"
    farm = Farm.objects.get(pk=res.data["farm_id"])
    assert farm.org_id == world["org"].org_id
    assert farm.village_id == world["village"].pk
    assert _audit("FARM_CREATED", world["org"].org_id).count() == 1

    for role in ("manager", "engineer", "worker"):
        assert _as(world, role).post("/api/v1/farms/", body, format="json").status_code == 403

    client = _as(world, "owner")
    assert client.post("/api/v1/farms/", {**body, "name": "  "}, format="json").status_code == 400
    assert client.post(
        "/api/v1/farms/", {**body, "province_id": 999999}, format="json"
    ).status_code == 400
    assert client.post(
        "/api/v1/farms/", {**body, "city_id": world["far_city"].pk}, format="json"
    ).status_code == 400
    assert client.post(
        "/api/v1/farms/", {**body, "village_id": world["far_village"].pk}, format="json"
    ).status_code == 400
    assert client.post(
        "/api/v1/farms/", {"name": "x", "city_id": world["city"].pk}, format="json"
    ).status_code == 400
    # The remaining verdicts the endpoint advertises — each one a distinct
    # message, not a generic 400: a missing city, an unknown city, a village that
    # is not an integer, and an unknown village.
    assert client.post(
        "/api/v1/farms/", {"name": "x", "province_id": world["province"].pk}, format="json"
    ).status_code == 400
    assert client.post(
        "/api/v1/farms/", {**body, "city_id": 999999}, format="json"
    ).status_code == 400
    assert client.post(
        "/api/v1/farms/", {**body, "village_id": "abc"}, format="json"
    ).status_code == 400
    assert client.post(
        "/api/v1/farms/", {**body, "village_id": 999999}, format="json"
    ).status_code == 400


def test_farm_update_is_owner_only_and_scope_narrowed():
    world = _make_world()
    url = f"/api/v1/farms/{world['farm'].pk}/"

    assert _as(world, "manager").patch(url, {"name": "nope"}, format="json").status_code == 403
    assert (
        _as(world, "owner")
        .patch(f"/api/v1/farms/{world['foreign_farm'].pk}/", {"name": "nope"}, format="json")
        .status_code
        == 404
    )

    res = _as(world, "owner").patch(url, {"name": "Renamed"}, format="json")
    assert res.status_code == 200, res.content
    assert res.data["name"] == "Renamed"
    assert _audit("FARM_UPDATED", world["org"].org_id).count() == 1

    # A scope row pointing at OTHER resources narrows this owner out of THIS farm.
    AccountRoleScope.objects.create(
        user=world["users"]["owner"].user, org=world["org"], role="owner",
        farm=world["empty_farm"],
    )
    assert _as(world, "owner").patch(url, {"name": "again"}, format="json").status_code == 403
    assert (
        _as(world, "owner")
        .patch(f"/api/v1/farms/{world['empty_farm'].pk}/", {"name": "ok"}, format="json")
        .status_code
        == 200
    )


def test_delete_farm_refuses_until_empty():
    world = _make_world()
    url = f"/api/v1/farms/{world['farm'].pk}/"

    res = _as(world, "owner").delete(url)
    assert res.status_code == 409
    assert res.data["error"] == "farm not empty"

    assert _as(world, "manager").delete(url).status_code == 403
    assert (
        _as(world, "owner").delete(f"/api/v1/farms/{world['foreign_farm'].pk}/").status_code
        == 404
    )

    # The 409 that must NOT be written to the audit trail.
    assert _audit("FARM_DELETED", world["org"].org_id).count() == 0

    bare_url = f"/api/v1/farms/{world['bare_farm'].pk}/"
    res = _as(world, "owner").delete(bare_url)
    if connection.vendor == "postgresql":
        assert res.status_code == 200, res.content
    else:
        # sqlite has no reports/telemetry history tables, so the collector
        # cannot verify dependents — the API refuses instead of deleting blindly.
        assert res.status_code == 409, res.content
        assert "cannot be verified" in res.data["error"]
        return
    assert res.data == {"farm_id": world["bare_farm"].pk, "deleted": True}
    assert not Farm.objects.filter(pk=world["bare_farm"].pk).exists()
    # The audit row survives the entity it records (its farm FK is null).
    assert _audit("FARM_DELETED", world["org"].org_id).count() == 1


# ---------------------------------------------------------------------------
# F4/F5/F6 — saloons
# ---------------------------------------------------------------------------


def test_create_saloon_manager_plus():
    world = _make_world()
    url = f"/api/v1/farms/{world['farm'].pk}/saloons/"

    res = _as(world, "manager").post(url, {"name": "Saloon 9"}, format="json")
    assert res.status_code == 201, res.content
    assert res.data["name"] == "Saloon 9"
    assert res.data["farm_id"] == world["farm"].pk
    assert res.data["sensor_slots"] == []
    saloon = Saloon.objects.get(pk=int(res.data["saloon_id"]))
    assert saloon.org_id == world["org"].org_id
    assert _audit("SALOON_CREATED", world["org"].org_id).count() == 1

    assert _as(world, "engineer").post(url, {"name": "x"}, format="json").status_code == 403
    assert _as(world, "worker").post(url, {"name": "x"}, format="json").status_code == 403
    assert (
        _as(world, "manager")
        .post(f"/api/v1/farms/{world['foreign_farm'].pk}/saloons/", {"name": "x"}, format="json")
        .status_code
        == 404
    )
    assert _as(world, "manager").post(url, {"name": ""}, format="json").status_code == 400


def test_saloon_update_and_delete_guards():
    world = _make_world()
    url = f"/api/v1/saloons/{world['saloon'].pk}/"

    res = _as(world, "manager").patch(url, {"name": "Renamed"}, format="json")
    assert res.status_code == 200, res.content
    assert res.data["name"] == "Renamed"
    assert _audit("SALOON_UPDATED", world["org"].org_id).count() == 1
    assert _as(world, "engineer").patch(url, {"name": "x"}, format="json").status_code == 403
    assert _as(world, "manager").patch(url, {}, format="json").status_code == 400

    # Active flock → refuse, before anything else is even considered.
    res = _as(world, "manager").delete(url)
    assert res.status_code == 409
    assert res.data["error"] == "saloon has an active flock"

    # Foreign → 404 (never an oracle).
    assert (
        _as(world, "manager").delete(f"/api/v1/saloons/{world['foreign_saloon'].pk}/").status_code
        == 404
    )

    # A CLOSED flock is still history. `flock_cycles.saloon` is ON DELETE
    # RESTRICT, so Postgres refuses this delete forever — the sqlite loop hid it
    # because Django's collector only emulates the constraint there. The guard
    # now says so in the same words on every backend (see the service docstring).
    world["flock"].end_date = dj_tz.now()
    world["flock"].save(update_fields=["end_date"])
    res = _as(world, "manager").delete(url)
    assert res.status_code == 409, res.content
    assert res.data["error"] == "saloon has flock history"
    assert _audit("SALOON_DELETED", world["org"].org_id).count() == 0

    # A saloon that never housed a flock, but a position still references it →
    # the ProtectedError branch, a named 409 rather than a 500.
    fresh = Saloon.objects.create(
        org=world["org"], farm=world["bare_farm"], name=f"Fresh-{world['tag']}"
    )
    fresh_url = f"/api/v1/saloons/{fresh.pk}/"
    SaloonPosition.objects.create(saloon=fresh, org=world["org"], label="North Wall")
    res = _as(world, "manager").delete(fresh_url)
    assert res.status_code == 409, res.content
    if connection.vendor == "postgresql":
        assert "still referenced" in res.data["error"]
    assert _audit("SALOON_DELETED", world["org"].org_id).count() == 0

    SaloonPosition.objects.filter(saloon=fresh).delete()
    res = _as(world, "manager").delete(fresh_url)
    if connection.vendor != "postgresql":
        # Same sqlite pragmatism as the farm delete (unmanaged history tables).
        assert res.status_code == 409, res.content
        assert "cannot be verified" in res.data["error"]
        return
    assert res.status_code == 200, res.content
    assert not Saloon.objects.filter(pk=fresh.pk).exists()
    assert _audit("SALOON_DELETED", world["org"].org_id).count() == 1


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="saloon_telemetry is Postgres-only; the guard degrades to allow elsewhere",
)
def test_saloon_delete_refuses_when_measured():
    """The third F6 verdict: a saloon that ever reported telemetry is never removable.

    Postgres-only by construction — `saloon_has_telemetry` returns False off
    Postgres (TELEMETRY_UNMANAGED_NOTE), so this is the one branch the sqlite
    loop cannot exercise. No flock here, so the check reaches the telemetry rule.
    """
    world = _make_world()
    fresh = Saloon.objects.create(
        org=world["org"], farm=world["bare_farm"], name=f"Measured-{world['tag']}"
    )
    # The hypertable's slot_id is a real FK on Postgres, so the row needs a
    # position and a slot before it can be inserted at all.
    position = SaloonPosition.objects.create(saloon=fresh, org=world["org"], label="North")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"ft-slot-{world['tag']}", saloon=fresh, org=world["org"], position=position
    )
    SaloonTelemetry.objects.create(
        org=world["org"],
        saloon=fresh,
        slot=slot,
        device_ts="2026-09-01T06:00:00+00:00",
        server_received_ts="2026-09-01T06:00:10+00:00",
        metrics={"temp": {"avg": 24.0}},
        is_buffered=False,
    )

    res = _as(world, "manager").delete(f"/api/v1/saloons/{fresh.pk}/")
    assert res.status_code == 409, res.content
    assert res.data["error"] == "saloon has telemetry history"
    assert Saloon.objects.filter(pk=fresh.pk).exists()
    assert _audit("SALOON_DELETED", world["org"].org_id).count() == 0


# ---------------------------------------------------------------------------
# F7/F8 — positions
# ---------------------------------------------------------------------------


def test_position_create_rename_and_duplicate_label():
    world = _make_world()
    url = f"/api/v1/saloons/{world['saloon'].pk}/positions/"

    res = _as(world, "manager").post(url, {"label": "North Wall"}, format="json")
    assert res.status_code == 201, res.content
    position_id = res.data["position_id"]
    assert res.data["created_by"] == world["users"]["manager"].user.pk
    assert _audit("POSITION_CREATED", world["org"].org_id).count() == 1

    res = _as(world, "manager").post(url, {"label": "North Wall"}, format="json")
    assert res.status_code == 409
    assert _as(world, "engineer").post(url, {"label": "x"}, format="json").status_code == 403
    assert _as(world, "manager").post(url, {"label": ""}, format="json").status_code == 400

    detail = f"/api/v1/saloons/{world['saloon'].pk}/positions/{position_id}/"
    res = _as(world, "manager").patch(detail, {"label": "South Wall"}, format="json")
    assert res.status_code == 200, res.content
    assert res.data["label"] == "South Wall"
    assert SaloonPosition.objects.get(pk=position_id).label == "South Wall"
    assert _audit("POSITION_UPDATED", world["org"].org_id).count() == 1

    SaloonPosition.objects.create(saloon=world["saloon"], org=world["org"], label="Other")
    assert (
        _as(world, "manager").patch(detail, {"label": "Other"}, format="json").status_code == 409
    )
    assert _as(world, "manager").patch(detail, {}, format="json").status_code == 400
    assert (
        _as(world, "manager")
        .patch(
            f"/api/v1/saloons/{world['saloon'].pk}/positions/999999/",
            {"label": "x"},
            format="json",
        )
        .status_code
        == 404
    )


# ---------------------------------------------------------------------------
# F14 — close the cycle
# ---------------------------------------------------------------------------


def test_flock_close_sets_end_date_and_rerarms(monkeypatch):
    world = _make_world()
    calls: list = []
    monkeypatch.setattr(
        "apps.alerts.signals.cache_saloon_thresholds.delay",
        lambda slot: calls.append(slot),
    )
    url = f"/api/v1/saloons/{world['saloon'].pk}/flocks/{world['flock'].pk}/close/"

    assert _as(world, "worker").patch(url, {"end_date": "2026-09-12"}, format="json").status_code == 403
    assert (
        _as(world, "engineer")
        .patch(
            f"/api/v1/saloons/{world['saloon'].pk}/flocks/{world['other_flock'].pk}/close/",
            {"end_date": "2026-09-12"},
            format="json",
        )
        .status_code
        == 400
    )
    assert _as(world, "engineer").patch(url, {"end_date": "nonsense"}, format="json").status_code == 400

    start_day = world["flock"].start_date.astimezone(TEHRAN).date()
    res = _as(world, "engineer").patch(
        url, {"end_date": (start_day - timedelta(days=1)).isoformat()}, format="json"
    )
    assert res.status_code == 400
    assert "start_date" in res.data["error"]

    today = datetime.now(TEHRAN).date()
    res = _as(world, "engineer").patch(url, {"end_date": today.isoformat()}, format="json")
    assert res.status_code == 200, res.content
    assert res.data["status"] == "CLOSED"
    assert res.data["end_date"] == today.isoformat()

    world["flock"].refresh_from_db()
    assert world["flock"].end_date.astimezone(TEHRAN).date() == today
    # The v0.4 Q3 rule: a cycle ending mid-day re-arms the saloon's hash NOW.
    assert str(world["saloon"].pk) in calls
    assert _audit("FLOCK_CLOSED", world["org"].org_id).count() == 1

    res = _as(world, "engineer").patch(url, {"end_date": today.isoformat()}, format="json")
    assert res.status_code == 400
    assert res.data["error"] == "no active flock"


# ---------------------------------------------------------------------------
# F15 — direct mortality entry (and the KPI it feeds)
# ---------------------------------------------------------------------------


def test_mortality_entry_moves_the_kpi_series():
    world = _make_world()
    url = f"/api/v1/saloons/{world['saloon'].pk}/flocks/{world['flock'].pk}/mortality/"
    today = datetime.now(TEHRAN).date()

    kpi = f"/api/v1/saloons/{world['saloon'].pk}/kpis/?metrics=mortality_pct"
    before = _as(world, "engineer").get(kpi)
    assert before.status_code == 200, before.content
    assert before.data["mortality_pct"] == 0

    assert _as(world, "worker").post(url, {"occurred_on": today.isoformat(), "count": 1}, format="json").status_code == 403
    assert (
        _as(world, "engineer")
        .post(url, {"occurred_on": today.isoformat(), "count": 0}, format="json")
        .status_code
        == 400
    )
    assert (
        _as(world, "engineer")
        .post(
            url,
            {"occurred_on": (today + timedelta(days=1)).isoformat(), "count": 1},
            format="json",
        )
        .status_code
        == 400
    )
    start_day = world["flock"].start_date.astimezone(TEHRAN).date()
    assert (
        _as(world, "engineer")
        .post(
            url,
            {"occurred_on": (start_day - timedelta(days=1)).isoformat(), "count": 1},
            format="json",
        )
        .status_code
        == 400
    )

    res = _as(world, "engineer").post(
        url,
        {
            "occurred_on": today.isoformat(),
            "count": 15,
            "cause": "heat_stress",
            "note": "fan outage",
        },
        format="json",
    )
    assert res.status_code == 201, res.content
    assert res.data["count"] == 15
    row = MortalityRecord.objects.get(pk=res.data["mortality_id"])
    assert row.flock_id == world["flock"].pk
    assert row.saloon_id == world["saloon"].pk
    assert row.org_id == world["org"].org_id
    assert row.recorded_by_id == world["users"]["engineer"].user.pk
    assert row.occurred_at.astimezone(TEHRAN).date() == today
    assert row.occurred_at.astimezone(TEHRAN).time() == dt_time.min
    assert row.notes == "fan outage"
    assert _audit("MORTALITY_RECORDED", world["org"].org_id).count() == 1

    after = _as(world, "engineer").get(kpi)
    assert after.data["mortality_pct"] == 15.0
    assert after.data["daily_mortality_pct"][-1]["date"] == today.isoformat()

    # Another saloon's flock is not this saloon's flock.
    assert (
        _as(world, "engineer")
        .post(
            f"/api/v1/saloons/{world['saloon'].pk}/flocks/{world['other_flock'].pk}/mortality/",
            {"occurred_on": today.isoformat(), "count": 1},
            format="json",
        )
        .status_code
        == 400
    )


# ---------------------------------------------------------------------------
# F9 — org alert feed
# ---------------------------------------------------------------------------


def _alert(world, saloon, *, metric="nh3", tier=2, resolved=False):
    return AlertInstance.objects.create(
        org=world["org"],
        farm=saloon.farm,
        saloon=saloon,
        metric_type=metric,
        current_status="RESOLVED" if resolved else f"ACTIVE_TIER_{tier}",
        current_value=27.5,
        highest_tier_reached=tier,
        resolved_at=dj_tz.now() if resolved else None,
    )


def test_org_alert_feed_filters_and_role():
    world = _make_world()
    active = _alert(world, world["saloon"])
    resolved = _alert(world, world["saloon"], metric="temp", tier=1, resolved=True)
    other = _alert(world, world["other_saloon"], metric="co2", tier=3)
    foreign = AlertInstance.objects.create(
        org=world["other"],
        farm=world["foreign_farm"],
        saloon=world["foreign_saloon"],
        metric_type="temp",
        current_status="ACTIVE_TIER_1",
        highest_tier_reached=1,
    )

    res = _as(world, "engineer").get("/api/v1/alerts/")
    assert res.status_code == 200, res.content
    ids = {row["alert_id"] for row in res.data["results"]}
    assert ids == {str(active.id), str(other.id)}
    assert res.data["count"] == 2
    assert str(foreign.id) not in ids

    res = _as(world, "engineer").get("/api/v1/alerts/?status=ALL")
    assert {row["alert_id"] for row in res.data["results"]} == {
        str(active.id),
        str(resolved.id),
        str(other.id),
    }
    res = _as(world, "engineer").get(f"/api/v1/alerts/?saloon_id={world['saloon'].pk}")
    assert {row["alert_id"] for row in res.data["results"]} == {str(active.id)}
    res = _as(world, "engineer").get(f"/api/v1/alerts/?farm_id={world['farm'].pk}")
    assert {row["alert_id"] for row in res.data["results"]} == {str(active.id)}
    res = _as(world, "engineer").get("/api/v1/alerts/?tier=3")
    assert {row["alert_id"] for row in res.data["results"]} == {str(other.id)}
    res = _as(world, "engineer").get("/api/v1/alerts/?metric=nh3")
    assert {row["alert_id"] for row in res.data["results"]} == {str(active.id)}
    res = _as(world, "engineer").get("/api/v1/alerts/?limit=1")
    assert len(res.data["results"]) == 1 and res.data["count"] == 2

    client = _as(world, "engineer")
    assert client.get("/api/v1/alerts/?tier=9").status_code == 400
    assert client.get("/api/v1/alerts/?metric=nope").status_code == 400
    assert client.get("/api/v1/alerts/?status=MAYBE").status_code == 400
    assert client.get("/api/v1/alerts/?limit=-1").status_code == 400
    assert _as(world, "worker").get("/api/v1/alerts/").status_code == 403


# ---------------------------------------------------------------------------
# F10 — org home rollup
# ---------------------------------------------------------------------------


def test_overview_rollup_and_scope_narrowing():
    world = _make_world()
    _alert(world, world["saloon"], tier=2)
    _alert(world, world["other_saloon"], tier=1)
    _alert(world, world["saloon"], metric="temp", tier=3, resolved=True)

    res = _as(world, "worker").get("/api/v1/overview/")
    assert res.status_code == 200, res.content
    by_id = {row["farm_id"]: row for row in res.data["farms"]}
    assert by_id[world["farm"].pk]["saloon_count"] == 1
    assert by_id[world["farm"].pk]["active_alert_count"] == 1
    assert by_id[world["farm"].pk]["highest_tier"] == 2
    assert by_id[world["empty_farm"].pk]["saloon_count"] == 1
    # Three farms in the org; the bare one contributes zeros, never nulls.
    assert res.data["totals"]["farm_count"] == 3
    assert res.data["totals"]["saloon_count"] == 2
    assert res.data["totals"]["active_alert_count"] == 2
    assert res.data["totals"]["highest_tier"] == 2
    assert by_id[world["bare_farm"].pk]["saloon_count"] == 0
    assert by_id[world["bare_farm"].pk]["highest_tier"] is None

    AccountRoleScope.objects.create(
        user=world["users"]["worker"].user, org=world["org"], role="worker",
        farm=world["farm"],
    )
    res = _as(world, "worker").get("/api/v1/overview/")
    assert [row["farm_id"] for row in res.data["farms"]] == [world["farm"].pk]
    assert res.data["totals"]["farm_count"] == 1
    assert res.data["totals"]["saloon_count"] == 1


# ---------------------------------------------------------------------------
# F11/F12/F13 — maintenance mutes
# ---------------------------------------------------------------------------


def test_maintenance_mute_crud_and_rules():
    world = _make_world()
    url = "/api/v1/maintenance-mutes/"
    now = dj_tz.now()

    assert _as(world, "engineer").get(url).status_code == 403
    assert _as(world, "worker").post(url, {}, format="json").status_code == 403

    body = {
        "saloon_id": world["saloon"].pk,
        "starts_at": (now + timedelta(hours=1)).isoformat(),
        "ends_at": (now + timedelta(hours=3)).isoformat(),
        "reason": "Fan replacement",
    }
    res = _as(world, "manager").post(url, body, format="json")
    assert res.status_code == 201, res.content
    mute_id = res.data["mute_id"]
    assert res.data["active"] is False  # starts in an hour

    client = _as(world, "manager")
    assert client.post(url, {**body, "starts_at": "nonsense"}, format="json").status_code == 400
    assert client.post(
        url, {**body, "ends_at": body["starts_at"]}, format="json"
    ).status_code == 400
    assert client.post(
        url,
        {**body, "ends_at": (now + timedelta(hours=100)).isoformat()},
        format="json",
    ).status_code == 400
    assert client.post(
        url,
        {
            **body,
            "starts_at": (now - timedelta(hours=5)).isoformat(),
            "ends_at": (now - timedelta(hours=2)).isoformat(),
        },
        format="json",
    ).status_code == 400
    assert client.post(url, {**body, "reason": ""}, format="json").status_code == 400
    assert client.post(
        url, {**body, "saloon_id": world["foreign_saloon"].pk}, format="json"
    ).status_code == 404
    # Overlap with the window just created.
    overlap = {
        **body,
        "starts_at": (now + timedelta(hours=2)).isoformat(),
        "ends_at": (now + timedelta(hours=4)).isoformat(),
    }
    assert client.post(url, overlap, format="json").status_code == 409
    # Abutting (starts exactly when the other ends) is a legitimate schedule.
    abutting = {
        **body,
        "starts_at": (now + timedelta(hours=3)).isoformat(),
        "ends_at": (now + timedelta(hours=4)).isoformat(),
    }
    assert client.post(url, abutting, format="json").status_code == 201

    # An already-running window reads active, and the ENGINE agrees.
    running = {
        "saloon_id": world["saloon"].pk,
        "starts_at": (now - timedelta(minutes=30)).isoformat(),
        "ends_at": (now + timedelta(minutes=30)).isoformat(),
        "reason": "Running now",
    }
    res = client.post(url, running, format="json")
    assert res.status_code == 201, res.content
    assert res.data["active"] is True
    assert active_maintenance_mute(world["saloon"].pk, dj_tz.now()) is not None

    res = client.get(f"{url}?active_only=true")
    assert res.data["count"] == 1
    res = client.get(f"{url}?active_only=false")
    assert res.data["count"] == 3
    res = client.get(f"{url}?saloon_id={world['other_saloon'].pk}&active_only=false")
    assert res.data["count"] == 0
    assert client.get(f"{url}?active_only=maybe").status_code == 400

    # Revoke a NOT-YET-STARTED window: nothing suppressed, row removed.
    res = client.delete(f"{url}{mute_id}/")
    assert res.status_code == 200, res.content
    assert not MaintenanceMute.objects.filter(pk=mute_id).exists()
    # Revoke the RUNNING one: shortened in place.
    running_id = MaintenanceMute.objects.get(reason="Running now").pk
    res = client.delete(f"{url}{running_id}/")
    assert res.status_code == 200, res.content
    row = MaintenanceMute.objects.get(pk=running_id)
    assert row.ends_at <= dj_tz.now() + timedelta(seconds=5)
    assert row.ends_at > row.starts_at
    assert _audit("MUTE_REVOKED", world["org"].org_id).count() == 2
    # Already ended → 409, not a silent success.
    assert client.delete(f"{url}{running_id}/").status_code == 409
    assert client.delete(f"{url}999999/").status_code == 404
    assert _audit("MUTE_CREATED", world["org"].org_id).count() == 3


# ---------------------------------------------------------------------------
# Member role change (PATCH /org/members/{id}/)
# ---------------------------------------------------------------------------


def test_member_role_change_owner_only_and_revokes_sessions():
    world = _make_world()
    engineer = world["users"]["engineer"]
    # A live refresh line for that member (from a real login).
    _client_for(engineer)
    session = UserSession.objects.filter(user=engineer.user, revoked_at__isnull=True).first()
    assert session is not None

    url = f"/api/v1/org/members/{engineer.pk}/"
    assert _as(world, "manager").patch(url, {"role": "manager"}, format="json").status_code == 403
    client = _as(world, "owner")
    assert client.patch(url, {"role": "nope"}, format="json").status_code == 400
    assert client.patch(url, {"role": "manager"}, format="json").status_code == 200
    assert client.patch(url, {"role": "manager"}, format="json").status_code == 409
    own = world["users"]["owner"]
    assert (
        client.patch(f"/api/v1/org/members/{own.pk}/", {"role": "worker"}, format="json").status_code
        == 409
    )
    assert (
        client.patch("/api/v1/org/members/999999/", {"role": "worker"}, format="json").status_code
        == 404
    )
    foreign = world["foreign_owner"]
    assert client.patch(f"/api/v1/org/members/{foreign.pk}/", {"role": "worker"}, format="json").status_code == 404

    engineer.refresh_from_db()
    assert engineer.role == "manager"
    account = _audit("MEMBER_ROLE_CHANGED", world["org"].org_id).get()
    assert account.details["old_role"] == "engineer"
    assert account.details["new_role"] == "manager"
    session.refresh_from_db()
    assert session.revoked_at is not None


def test_backfill_command_is_backend_aware(capsys):
    """The refactored command must not die on a backend without the table."""
    from django.core.management import call_command

    call_command("backfill_legacy_slugs", verbosity=0)
    out = capsys.readouterr().out
    if connection.vendor != "postgresql":
        assert "Postgres-only" in out


def test_reviewer_role_is_now_grantable():
    """The v1.5 reports wave defined `reviewer` but no API could produce one."""
    world = _make_world()
    client = _as(world, "owner")
    res = client.post(
        "/api/v1/org/members/",
        {"username": f"ft-newreviewer-{world['tag']}", "role": "reviewer"},
        format="json",
    )
    assert res.status_code == 201, res.content
    assert res.data["role"] == "reviewer"

    member = OrgMember.objects.get(pk=res.data["member_id"])
    assert (
        client.patch(f"/api/v1/org/members/{member.pk}/", {"role": "worker"}, format="json").status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Compat slugs — Postgres only (legacy_slug_map exists solely as compat/0001 DDL)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _compat_ddl_guard():
    """PG: make sure legacy_slug_map exists (test_m7_17's pattern)."""
    if connection.vendor == "postgresql":
        try:
            from tests.conftest import _execute_sql, _sql_from_migration
            from tests.conftest import BACKEND_DIR

            with connection.cursor() as cursor:
                if cursor.execute(
                    "SELECT to_regclass('public.legacy_slug_map') IS NULL"
                ).fetchone()[0]:
                    _execute_sql(
                        cursor,
                        _sql_from_migration(
                            BACKEND_DIR / "apps" / "compat" / "migrations" / "0001_legacy_slug_map.py"
                        ),
                    )
        except Exception:  # noqa: BLE001 — best-effort, the class skips on sqlite anyway
            pass
    yield


class TestCompatSlugMinting:
    """The write path must mint legacy_slug_map rows in the same transaction.

    Postgres-only: off PG the mint helpers are deliberately no-ops (the DDL is
    never created there), so the claim can only be proven where the table and
    the compat resolver both exist — CI.
    """

    @staticmethod
    def _skip_off_pg():
        if connection.vendor != "postgresql":
            pytest.skip("legacy_slug_map is Postgres-only (compat/0001 DDL)")

    def test_farm_and_saloon_creation_mint_their_slugs(self):
        from apps.compat.legacy_slugs import LegacySlugMap

        self._skip_off_pg()
        world = _make_world()
        client = _as(world, "owner")
        res = client.post(
            "/api/v1/farms/",
            {
                "name": "Slug Farm",
                "province_id": world["province"].pk,
                "city_id": world["city"].pk,
            },
            format="json",
        )
        assert res.status_code == 201, res.content
        farm_id = res.data["farm_id"]
        slug = LegacySlugMap.objects.filter(kind="farm", farm_id=farm_id).first()
        assert slug is not None and slug.slug == f"farm-{farm_id}"
        assert slug.org_id == world["org"].org_id

        manager = _as(world, "manager")
        res = manager.post(
            f"/api/v1/farms/{farm_id}/saloons/", {"name": "Slug Saloon"}, format="json"
        )
        assert res.status_code == 201, res.content
        saloon_id = int(res.data["saloon_id"])
        row = LegacySlugMap.objects.filter(kind="device", saloon_id=saloon_id).first()
        assert row is not None and row.slug == f"device-{saloon_id}"
        assert row.farm_id == farm_id

        # The command is now the REPAIR tool for rows that predate the write
        # path: an ORM-created farm has no slug until it runs, and re-running
        # changes nothing.
        from django.core.management import call_command

        orphan = Farm.objects.create(
            org=world["org"],
            name="Orphan",
            province=world["province"],
            city=world["city"],
        )
        assert not LegacySlugMap.objects.filter(farm_id=orphan.pk).exists()
        call_command("backfill_legacy_slugs", verbosity=0)
        assert LegacySlugMap.objects.filter(kind="farm", farm_id=orphan.pk).exists()
        before = LegacySlugMap.objects.count()
        call_command("backfill_legacy_slugs", verbosity=0)
        assert LegacySlugMap.objects.count() == before

        # And the Flutter alias resolves the brand-new farm right away.
        org_slug = LegacySlugMap.objects.filter(kind="org", org_id=world["org"].org_id).first()
        assert org_slug is not None
        assert f"farm-{farm_id}" in set(
            LegacySlugMap.objects.filter(kind="farm").values_list("slug", flat=True)
        )
