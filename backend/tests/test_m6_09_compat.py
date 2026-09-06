"""M6-09 acceptance — the Flutter compat layer (v1.6.0-draft, spec §10).

Acceptance mapping (docs/api-compat-flutter-v1.md §3 route table + §10):
- C1..C15 REST   → exact-Flutter-key assertions (raw arrays, {farm_history: [...]})
- C16 WS shim    → connection_established, flat-record push, buffered suppressed,
                   4003 foreign slug / non-member / no token, 4000 over ws:active
- backfill       → idempotent (second run creates zero), stable suggested slugs
- error contract → {"error": ...} envelopes; foreign slug is 404, never 500;
                   bad charset 404; inverted window 400

Posture: REST view tests run wherever the suite runs (views are same-thread;
uncommitted world rows are visible; the tenant middleware arms the GUC the
same way it does for every /api/v1 call). The WS shim tests are
transaction=True (M6-06 lesson: Channels runs the sync consumer in an
executor thread with its OWN connection). The composite drain→shim beat is
split deliberately: m6-06 proves the producer emits the core event to the
right saloon group; the direct group_send tests here prove the shim
translates exactly that event — a true end-to-end over InMemoryChannelLayer
would be a lie (its per-loop queues cannot cross the drain's executor loop),
so the honest composition is these two proofs sharing one event shape.

فارسی: پذیرش لایهٔ سازگاری — کلیدهای دقیقِ فلاتر، خطای ۴۰۴ برای اسلاگِ خارجی،
سوکتِ مزرعه با همان سقف و کدهای بستنِ هسته.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

import pytest
import redis as redis_lib
from channels.layers import InMemoryChannelLayer
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.compat.models import LegacySlugMap
from apps.compat.services_compat import compat_flatten_reading
from apps.devices.models import SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry import ws
from apps.telemetry.models import SaloonTelemetry
from apps.tenants.models import Organization

from tests.conftest import INTEGRATION_REDIS_URL

PASSWORD = "strong-pass-123"
CANON = "/api/compat/v1"
LEGACY = "/api"


def _pg():
    return connection.vendor == "postgresql"


# ---------------------------------------------------------------------------
# Fixtures — redis isolation + InMemory layer + cascade teardown + slug DDL
# ---------------------------------------------------------------------------


class _FakeLayers:
    """channel_layers singleton stand-in holding one InMemory layer (m6-06).
    __getitem__/__contains__ are REQUIRED: get_channel_layer() probes with
    `in` first — without them Python's legacy __getitem__ iteration protocol
    answers 'missing' and Channels builds the REAL redis backend, whose
    group_add then blocks the consumer forever (zero-output hang)."""

    def __init__(self, layer):
        self.backends = {"default": layer}

    def __getitem__(self, alias):
        return self.backends[alias]

    def __contains__(self, alias):
        return alias in self.backends


class _RecordingLayer:
    """Sync-capturing fake layer for the drain (producer) assertion."""

    def __init__(self):
        self.sent = []

    async def group_send(self, group, message):
        self.sent.append((group, dict(message)))

    async def group_add(self, group, channel):
        pass

    async def group_discard(self, group, channel):
        pass


@pytest.fixture(autouse=True)
def _compat_stack(monkeypatch):
    """Everything the compat surface touches, isolated: cap keys (Redis db11),
    channel fan-out (InMemory), the conftest cascade-flush patch (PG teardown
    of transactional tests truncates FK-referenced tables — needs CASCADE),
    and the legacy_slug_map DDL replay (unmanaged → --nomigrations never
    creates it; the RunSQL migration does not run under test settings)."""
    if _pg():
        from tests.conftest import _install_cascade_flush

        _install_cascade_flush(connection)
        with connection.cursor() as cursor:
            missing = cursor.execute(
                "SELECT to_regclass('public.legacy_slug_map') IS NULL"
            ).fetchone()[0]
            if missing:
                from tests.conftest import (
                    BACKEND_DIR,
                    _execute_sql,
                    _sql_from_migration,
                )

                _execute_sql(
                    cursor,
                    _sql_from_migration(
                        BACKEND_DIR / "apps" / "compat" / "migrations" / "0001_legacy_slug_map.py"
                    ),
                )
    else:
        # sqlite quick loop: the PG-flavored RunSQL DDLs (UUID, TIMESTAMPTZ,
        # hypertable) are not portable, so replay portable shapes carrying the
        # exact columns the ORM touches — test.py's stated intent ("keep the
        # model available in fast local tests by creating its table from
        # state"), implemented here for the compat REST surface. CHECK
        # constraints and indexes live in the real migrations; tests write
        # valid kinds.
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS legacy_slug_map ("
                "slug TEXT PRIMARY KEY, kind TEXT NOT NULL, org_id VARCHAR(32) NOT NULL, "
                "farm_id INTEGER, saloon_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS saloon_telemetry ("
                "server_received_ts TIMESTAMP NOT NULL PRIMARY KEY, device_ts TIMESTAMP NOT NULL, "
                "org_id VARCHAR(32) NOT NULL, saloon_id INTEGER NOT NULL, slot_id VARCHAR(50) NOT NULL, "
                "is_buffered BOOLEAN, clock_drift BOOLEAN, metrics TEXT NOT NULL)"
            )
    client = redis_lib.Redis.from_url(INTEGRATION_REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.ws._redis_client", client)
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    layer = InMemoryChannelLayer(expiry=60.0)
    monkeypatch.setattr("channels.layers.channel_layers", _FakeLayers(layer))
    yield client


# ---------------------------------------------------------------------------
# World builder — topology + slugs via the REAL backfill command
# ---------------------------------------------------------------------------


def _make_world(*, saloons=("A1", "B2")):
    tag = uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon_rows = [
        Saloon.objects.create(org=org, farm=farm, name=f"s-{name}-{tag}") for name in saloons
    ]
    for index, saloon in enumerate(saloon_rows):
        position = SaloonPosition.objects.create(saloon=saloon, org=org, label=f"pos-{index}")
        SaloonSensorSlot.objects.create(
            slot_id=f"slot-{tag}-{index}",
            saloon=saloon,
            org=org,
            position=position,
            allowed_metric_keys=["temp", "humidity"],
        )

    other_org = Organization.objects.create(name=f"other-{tag}")
    other_farm = Farm.objects.create(org=other_org, name=f"of-{tag}", province=province, city=city)
    other_saloon = Saloon.objects.create(org=other_org, farm=other_farm, name=f"os-{tag}")

    call_command("backfill_legacy_slugs", verbosity=0)

    user_model = get_user_model()
    people = {}
    for role in ("owner", "engineer", "worker"):
        user = user_model.objects.create_user(username=f"{role}-{tag}", password=PASSWORD)
        OrgMember.objects.create(user=user, org=org, role=role, is_active=True)
        people[role] = user
    stranger = user_model.objects.create_user(username=f"str-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=stranger, org=other_org, role="owner", is_active=True)
    people["stranger"] = stranger

    by_kind = {"org": {}, "farm": {}, "device": {}}
    for row in LegacySlugMap.objects.filter(org_id__in=[org.org_id, other_org.org_id]):
        key = (
            row.farm_id if row.kind == "farm"
            else (row.saloon_id if row.kind == "device" else str(row.org_id))
        )
        by_kind[row.kind][key] = row.slug

    return {
        "tag": tag,
        "org": org,
        "farm": farm,
        "saloons": saloon_rows,
        "saloon": saloon_rows[0],
        "other_org": other_org,
        "other_farm": other_farm,
        "other_saloon": other_saloon,
        "people": people,
        "slugs": {
            "org": by_kind["org"][str(org.org_id)],
            "org_other": by_kind["org"][str(other_org.org_id)],
            "farm": by_kind["farm"][farm.pk],
            "farm_other": by_kind["farm"][other_farm.pk],
            "devices": [by_kind["device"][s.pk] for s in saloon_rows],
            "device": by_kind["device"][saloon_rows[0].pk],
        },
    }


def _login(username):
    client = APIClient()
    response = client.post(
        reverse("accounts:auth-login"),
        {"username": username, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.data
    return response.data["access_token"]


def _client_for(world, role):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_login(world['people'][role].username)}")
    return client


def _seed_telemetry(world, per_saloon=3):
    """A few committed windows per saloon; reads are newest-first."""
    base = datetime(2026, 9, 6, 10, 0, tzinfo=dt_timezone.utc)
    rows = []
    for index, saloon in enumerate(world["saloons"]):
        for step in range(per_saloon):
            rows.append(SaloonTelemetry(
                server_received_ts=base + timedelta(minutes=step, seconds=index),
                device_ts=base + timedelta(minutes=step - 1, seconds=index),
                org=world["org"],
                saloon=saloon,
                slot_id=f"slot-{world['tag']}-{index}",
                is_buffered=False,
                metrics={"temp": {"avg": 24.5 + step}, "humidity": {"avg": 55.0}},
            ))
    return SaloonTelemetry.objects.bulk_create(rows)


def _other_device_slug(world):
    return LegacySlugMap.objects.get(org_id=world["other_org"].org_id, kind="device").slug


# ---------------------------------------------------------------------------
# Backfill (the write path)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_backfill_is_idempotent_with_suggested_slugs(_compat_stack):
    world = _make_world()
    assert LegacySlugMap.objects.get(slug=world["slugs"]["org"]).kind == "org"
    assert LegacySlugMap.objects.get(slug=world["slugs"]["farm"]).kind == "farm"
    for saloon, slug in zip(world["saloons"], world["slugs"]["devices"]):
        row = LegacySlugMap.objects.get(slug=slug)
        assert row.kind == "device" and row.saloon_id == saloon.pk and row.farm_id == saloon.farm_id

    before = LegacySlugMap.objects.count()
    call_command("backfill_legacy_slugs", verbosity=0)
    assert LegacySlugMap.objects.count() == before  # re-run creates ZERO rows


# ---------------------------------------------------------------------------
# C1..C4 — orgs
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_c1_orgs_list_raw_array(_compat_stack):
    world = _make_world()
    body = _client_for(world, "owner").get(f"{CANON}/orgs/").json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["slug"] == world["slugs"]["org"]
    assert body[0]["name"] == world["org"].name
    assert set(body[0]) == {"slug", "name", "created_at"}


@pytest.mark.django_db
def test_c2_org_detail_and_foreign_slug_404(_compat_stack):
    world = _make_world()
    response = _client_for(world, "engineer").get(f"{CANON}/orgs/{world['slugs']['org']}")
    assert response.status_code == 200
    assert response.json()["slug"] == world["slugs"]["org"]

    foreign = _client_for(world, "stranger").get(f"{CANON}/orgs/{world['slugs']['org']}")
    assert foreign.status_code == 404
    assert foreign.json() == {"error": "org not found"}

    charset = _client_for(world, "owner").get(f"{CANON}/orgs/bad~slug!")
    assert charset.status_code == 404  # never 500, never an empty 200


@pytest.mark.django_db
def test_c3_c4_org_farms_and_devices(_compat_stack):
    world = _make_world()
    client = _client_for(world, "worker")
    farms = client.get(f"{CANON}/orgs/{world['slugs']['org']}/farms").json()
    assert [f["slug"] for f in farms] == [world["slugs"]["farm"]]
    assert farms[0]["organization_slug"] == world["slugs"]["org"]

    devices = client.get(f"{CANON}/orgs/{world['slugs']['org']}/devices").json()
    assert [d["slug"] for d in devices] == world["slugs"]["devices"]
    assert devices[0]["sensor_capabilities"] == ["humidity", "temperature"]  # mapped names
    assert devices[0]["org_slug"] == devices[0]["organization_slug"] == world["slugs"]["org"]
    assert devices[0]["firmware_version"] == ""
    assert devices[0]["last_seen_at"] == ""  # no telemetry yet


# ---------------------------------------------------------------------------
# C5..C9 — farms + devices
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_c5_to_c9_farms_and_devices_surface(_compat_stack):
    world = _make_world()
    client = _client_for(world, "owner")

    farms = client.get(f"{CANON}/farms/").json()
    assert [f["slug"] for f in farms] == [world["slugs"]["farm"]]

    farm = client.get(f"{CANON}/farms/{world['slugs']['farm']}").json()
    assert farm["name"] == world["farm"].name

    farm_devices = client.get(f"{CANON}/farms/{world['slugs']['farm']}/devices").json()
    assert [d["slug"] for d in farm_devices] == world["slugs"]["devices"]

    devices = client.get(f"{CANON}/devices/").json()
    assert [d["slug"] for d in devices] == world["slugs"]["devices"]

    device = client.get(f"{CANON}/devices/{world['slugs']['device']}").json()
    assert device["name"] == world["saloon"].name
    assert device["farm_slug"] == world["slugs"]["farm"]

    # foreign/unknown slugs are clean 404s for every resolver
    assert client.get(f"{CANON}/farms/{world['slugs']['farm_other']}").status_code == 404
    assert _client_for(world, "stranger").get(
        f"{CANON}/devices/{world['slugs']['device']}"
    ).status_code == 404
    assert client.get(f"{CANON}/devices/does-not-exist").status_code == 404


@pytest.mark.django_db
def test_legacy_aliases_hit_the_same_views(_compat_stack):
    world = _make_world()
    client = _client_for(world, "owner")
    assert client.get(f"{LEGACY}/orgs/").json() == client.get(f"{CANON}/orgs/").json()
    assert client.get(f"{LEGACY}/devices/{world['slugs']['device']}").status_code == 200
    # trailing-slash tolerant — no 301 (minimal clients don't follow redirects)
    assert client.get(f"{CANON}/orgs").status_code == 200


@pytest.mark.django_db
def test_unauthenticated_is_401(_compat_stack):
    _make_world()
    assert APIClient().get(f"{CANON}/orgs/").status_code == 401
    assert APIClient().get(f"{CANON}/devices/").status_code == 401


# ---------------------------------------------------------------------------
# C10..C15 — telemetry flat records
# ---------------------------------------------------------------------------

FLAT_KEYS = {
    "received_at", "sent_ts", "seq", "is_buffered", "clock_drift_ms",
    "temperature", "humidity", "lux", "nh3", "schema_version", "device_slug",
}


@pytest.mark.django_db
def test_c10_c14_latest_single_flat_record(_compat_stack):
    world = _make_world()
    _seed_telemetry(world)
    client = _client_for(world, "engineer")

    org_latest = client.get(f"{CANON}/telemetry/orgs/{world['slugs']['org']}/latest").json()
    assert set(org_latest) == FLAT_KEYS
    assert org_latest["temperature"] == "26.5"  # newest window across the org
    assert org_latest["humidity"] == "55.0"
    assert org_latest["lux"] == "" and org_latest["nh3"] == ""  # never null
    assert org_latest["seq"] == ""
    assert org_latest["device_slug"] in world["slugs"]["devices"]

    device_latest = client.get(
        f"{CANON}/telemetry/devices/{world['slugs']['device']}/latest"
    ).json()
    assert device_latest["device_slug"] == world["slugs"]["device"]
    assert device_latest["is_buffered"] is False
    # clock_drift_ms: received − sent = the 60 s window → 60000
    assert device_latest["clock_drift_ms"] == "60000"

    farm_latest = client.get(f"{CANON}/telemetry/farms/{world['slugs']['farm']}/latest").json()
    assert farm_latest["received_at"] == org_latest["received_at"]

    # empty saloon → the spec's 404, not an empty 200
    response = _client_for(world, "stranger").get(
        f"{CANON}/telemetry/devices/{_other_device_slug(world)}/latest"
    )
    assert response.status_code == 404
    assert response.json() == {"error": "no telemetry"}


@pytest.mark.django_db
def test_c11_c15_history_windowing_and_order(_compat_stack):
    world = _make_world()
    _seed_telemetry(world, per_saloon=5)
    client = _client_for(world, "worker")

    history = client.get(
        f"{CANON}/telemetry/devices/{world['slugs']['device']}/history", {"limit": 3}
    ).json()
    assert isinstance(history, list) and len(history) == 3
    stamps = [row["received_at"] for row in history]
    assert stamps == sorted(stamps, reverse=True)  # newest first
    assert all(set(row) == FLAT_KEYS for row in history)

    offset_page = client.get(
        f"{CANON}/telemetry/devices/{world['slugs']['device']}/history",
        {"limit": 2, "offset": 1},
    ).json()
    assert [row["received_at"] for row in offset_page] == stamps[1:3]

    window = client.get(
        f"{CANON}/telemetry/devices/{world['slugs']['device']}/history",
        {"from_dt": "2026-09-06T10:02:00Z", "to_dt": "2026-09-06T10:04:00Z"},
    ).json()
    assert 1 <= len(window) <= 3
    assert all(row["received_at"] >= "2026-09-06T10:02" for row in window)
    assert all(row["received_at"] <= "2026-09-06T10:04:59" for row in window)

    epoch = client.get(
        f"{CANON}/telemetry/devices/{world['slugs']['device']}/history",
        {"from_dt": 1789000000000},
    )
    assert epoch.status_code == 200  # epoch-ms accepted on the way in

    inverted = client.get(
        f"{CANON}/telemetry/devices/{world['slugs']['device']}/history",
        {"from_dt": "2026-09-06T10:00:00Z", "to_dt": "2026-09-06T09:00:00Z"},
    )
    assert inverted.status_code == 400
    assert inverted.json() == {"error": "invalid time window"}


@pytest.mark.django_db
def test_c13_farm_history_envelope_and_default_limit_24(_compat_stack):
    world = _make_world()
    _seed_telemetry(world, per_saloon=30)  # 60 rows total — the default caps at 24
    client = _client_for(world, "owner")
    body = client.get(f"{CANON}/telemetry/farms/{world['slugs']['farm']}/history").json()
    assert set(body) == {"farm_history"}  # the EXACT key the chart reads
    assert len(body["farm_history"]) == 24
    assert all(row["device_slug"] in world["slugs"]["devices"] for row in body["farm_history"])


@pytest.mark.django_db
def test_c10_no_telemetry_is_404(_compat_stack):
    world = _make_world()
    client = _client_for(world, "owner")
    response = client.get(f"{CANON}/telemetry/orgs/{world['slugs']['org']}/latest")
    assert response.status_code == 404
    assert response.json() == {"error": "no telemetry"}


# ---------------------------------------------------------------------------
# C16 — the farm WS shim
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _shim_comm(slug, token, canonical=False):
    from apps.compat.consumers_compat import FarmTelemetryConsumer

    prefix = "/api/compat/v1" if canonical else ""
    communicator = WebsocketCommunicator(
        FarmTelemetryConsumer.as_asgi(), f"{prefix}/ws/farms/{slug}/telemetry?token={token}"
    )
    communicator.scope["url_route"] = {"kwargs": {"farm_slug": slug}}
    return communicator


def _core_event(world, buffered=False, temp=25.5):
    return {
        "type": "telemetry.update",
        "saloon_id": world["saloon"].pk,
        "slot_id": f"slot-{world['tag']}-0",
        "device_ts": "2026-09-06T10:00:00Z",
        "is_buffered": buffered,
        "metrics": {"temp": {"avg": temp}, "humidity": {"avg": 58.0}},
    }


@pytest.mark.django_db(transaction=True)
def test_c16_shim_connects_and_translates_push(_compat_stack):
    world = _make_world()
    client = _compat_stack
    user = world["people"]["owner"]
    token = _login(user.username)
    communicator = _shim_comm(world["slugs"]["farm"], token)

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected is True
        hello = await communicator.receive_json_from()
        assert hello["type"] == "connection_established"
        assert hello["farm_slug"] == world["slugs"]["farm"]
        assert hello["saloon_count"] == len(world["saloons"])

        # THE beat: a core fan-out (the exact event shape m6-06's drain emits)
        # lands as ONE flat record with the exact Flutter keys.
        from channels.layers import channel_layers

        layer = channel_layers.backends["default"]
        await layer.group_send(
            ws.group_name(str(world["org"].org_id), world["saloon"].pk),
            _core_event(world),
        )
        flat = await communicator.receive_json_from()
        assert flat["temperature"] == "25.5"
        assert flat["humidity"] == "58.0"
        assert flat["device_slug"] == world["slugs"]["device"]
        assert flat["is_buffered"] is False
        assert flat["lux"] == "" and flat["nh3"] == ""

        # subscribe/unsubscribe accepted, TTL refreshed, no error back
        client.expire(ws.active_set_key(user.pk), 100)
        await communicator.send_json_to({"action": "subscribe", "topic": "x"})
        assert await communicator.receive_nothing(timeout=0.2)
        assert client.ttl(ws.active_set_key(user.pk)) > 90

        await communicator.disconnect()

    _run(scenario())


@pytest.mark.django_db(transaction=True)
def test_c16_shim_never_pushes_buffered_rows(_compat_stack):
    world = _make_world()
    token = _login(world["people"]["engineer"].username)
    communicator = _shim_comm(world["slugs"]["farm"], token)

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected is True
        await communicator.receive_json_from()  # hello

        from channels.layers import channel_layers

        layer = channel_layers.backends["default"]
        await layer.group_send(
            ws.group_name(str(world["org"].org_id), world["saloon"].pk),
            _core_event(world, buffered=True),
        )
        assert await communicator.receive_nothing(timeout=0.3)  # silence = enforced

        await communicator.disconnect()

    _run(scenario())


@pytest.mark.django_db(transaction=True)
def test_c16_shim_close_codes_4003_and_4000(_compat_stack):
    world = _make_world()
    client = _compat_stack
    user = world["people"]["worker"]
    cap = ws.config_int("ws_max_connections_per_user", 10)
    assert cap == 10

    codes = []
    from apps.compat.consumers_compat import FarmTelemetryConsumer

    original = FarmTelemetryConsumer.close

    def spy(consumer_self, code=None):
        codes.append(code)
        return original(consumer_self, code=code)

    foreign = _shim_comm(world["slugs"]["farm_other"], _login(user.username))
    unknown = _shim_comm("no-such-farm", _login(user.username))
    full = _shim_comm(world["slugs"]["farm"], _login(user.username))
    # and a missing token never even resolves
    no_token = WebsocketCommunicator(
        FarmTelemetryConsumer.as_asgi(),
        f"/ws/farms/{world['slugs']['farm']}/telemetry",
    )
    no_token.scope["url_route"] = {"kwargs": {"farm_slug": world["slugs"]["farm"]}}

    async def scenario():
        with mock.patch.object(FarmTelemetryConsumer, "close", spy):
            connected, _ = await foreign.connect()
        assert connected is False
        await foreign.disconnect()
        assert codes == [4003]  # another org's farm slug

        with mock.patch.object(FarmTelemetryConsumer, "close", spy):
            connected, _ = await unknown.connect()
        assert connected is False
        await unknown.disconnect()
        assert codes == [4003, 4003]

        for index in range(cap):
            client.sadd(ws.active_set_key(user.pk), f"ghost-{index}")
        with mock.patch.object(FarmTelemetryConsumer, "close", spy):
            connected, _ = await full.connect()
        assert connected is False
        await full.disconnect()
        assert codes == [4003, 4003, 4000]  # cap full

        with mock.patch.object(FarmTelemetryConsumer, "close", spy):
            connected, _ = await no_token.connect()
        assert connected is False
        await no_token.disconnect()
        assert codes == [4003, 4003, 4000, 4003]

    _run(scenario())


@pytest.mark.django_db(transaction=True)
def test_c16_canonical_prefix_serves_the_same_consumer(_compat_stack):
    world = _make_world()
    token = _login(world["people"]["worker"].username)
    communicator = _shim_comm(world["slugs"]["farm"], token, canonical=True)

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected is True
        hello = await communicator.receive_json_from()
        assert hello["type"] == "connection_established"
        await communicator.disconnect()

    _run(scenario())


# ---------------------------------------------------------------------------
# The producer side: the drain emits the exact event the shim translates (PG)
# ---------------------------------------------------------------------------


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_drain_emits_the_event_shape_the_shim_translates(timescale_db, _compat_stack):
    if not _pg():
        pytest.skip("saloon_telemetry is a TimescaleDB hypertable")
    import json

    from apps.devices.models import GatewaySensorMapping
    from apps.telemetry.consumers import STREAM_REALTIME, drain_stream
    from morghdari.subscriber.pipeline import handle_message

    world = _make_world()
    tag = world["tag"]
    slot = SaloonSensorSlot.objects.filter(saloon=world["saloon"]).first()
    GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot, org=world["org"]
    )

    recorder = _RecordingLayer()
    with mock.patch("apps.telemetry.ws.get_channel_layer", return_value=recorder):
        gateway_slot = f"GW-{tag}"
        topic = f"{world['org'].org_id}/farms/{world['farm'].pk}/gateways/{gateway_slot}/telemetry"
        payload = json.dumps({
            "gateway_hardware_id": f"hw-{tag}",
            "gateway_slot_id": gateway_slot,
            "schema_version": "2.0",
            "device_ts": "2026-09-06T11:00:00Z",
            "is_buffered": False,
            "readings": [{"pack_id": "P-A", "metrics": {"temp": {"avg": 26.25}}}],
        })
        assert handle_message(
            _compat_stack, topic, payload.encode(), db_fetch=lambda *a: slot.slot_id
        ) is None
        assert drain_stream(STREAM_REALTIME, count=10) >= 1

    (group, event) = recorder.sent[0]
    assert group == ws.group_name(str(world["org"].org_id), world["saloon"].pk)
    assert event["is_buffered"] is False  # the flag the shim's rule reads
    # translate the REAL event through the shim's adaptation — the composite proof
    from apps.compat.consumers_compat import _ShimRow

    flat = compat_flatten_reading(
        _ShimRow(event), world["slugs"]["device"]
    )
    assert flat["temperature"] == "26.25"
    assert flat["device_slug"] == world["slugs"]["device"]
    assert set(flat) == FLAT_KEYS


# ---------------------------------------------------------------------------
# Unit — the flattener itself (any backend)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_flat_record_from_db_row_shape(_compat_stack):
    class Row:  # duck-typed SaloonTelemetry
        server_received_ts = datetime(2026, 9, 5, 11, 15, tzinfo=dt_timezone.utc)
        device_ts = datetime(2026, 9, 5, 11, 14, 59, tzinfo=dt_timezone.utc)
        is_buffered = True
        buffered_delay_mins = 0.5  # the DB-generated column, when annotated
        metrics = {"temp": {"avg": 24.5}, "humidity": {"avg": 55}}

    flat = compat_flatten_reading(Row(), "device-a1")
    assert flat == {
        "received_at": "2026-09-05T11:15:00Z",
        "sent_ts": "2026-09-05T11:14:59Z",
        "seq": "",
        "is_buffered": True,
        "clock_drift_ms": "30000",
        "temperature": "24.5",
        "humidity": "55",
        "lux": "",
        "nh3": "",
        "schema_version": "1",
        "device_slug": "device-a1",
    }


@pytest.mark.django_db
def test_flat_record_from_raw_timestamp_strings(_compat_stack):
    """Shim events carry pre-formatted stamps — the flattener must not choke."""

    class Row:
        server_received_ts = "2026-09-06T10:00:00+00:00"
        device_ts = "2026-09-06T09:59:00Z"
        is_buffered = False
        buffered_delay_mins = None
        metrics = {"nh3": {"avg": 8}}

    flat = compat_flatten_reading(Row(), "device-x")
    assert flat["received_at"] == "2026-09-06T10:00:00+00:00"
    assert flat["sent_ts"] == "2026-09-06T09:59:00Z"
    assert flat["nh3"] == "8"
    assert flat["clock_drift_ms"] == "60000"  # parsed from the ts delta
