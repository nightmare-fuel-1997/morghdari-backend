"""M6-06/M6-07 acceptance — the Channels WebSocket surface.

Acceptance mapping (versions/v0.6/TASKS.md):
- M6-06 "two browsers see same update"     → the two-communicator group test
- M6-06 "cap closes 4000, 4003 = auth/org" → the close-code matrix (spy)
- M6-06 TTL refresh + zombie guard         → the ws:active Set/TTL tests
- M6-06 push after drain flush             → the real-pipeline drain test (PG)
- M6-07 reconnect gap-fill merge           → the gap_fill request/response tests

Posture: auth verdicts, cap Set bookkeeping, group fan-out (InMemory layer)
and TTL math run on ANY backend. The drain push and the gap-fill queries
touch the unmanaged saloon_telemetry hypertable, so they run Postgres-only
(use_migrations + timescale_db — the m3_05/m6_02 house pattern; sqlite has
neither the table nor set_config, mirroring test.py's pragmatism).

Async rules that keep this suite deterministic:
- one event loop per scenario (_run) — never mix loops across a
  communicator's queues;
- logins and communicator CONSTRUCTION happen in the sync test body (Django
  ORM + APIClient are sync-only), the scenario body runs pure I/O;
- every socket-opening test is django_db(transaction=True): Channels runs
  the sync consumer's connect() in an EXECUTOR THREAD with its own DB
  connection, so world rows must be COMMITTED (a TestCase atomic block
  would look empty there — the classic "works sync, denies async" trap);
- denied sockets are explicitly disconnect()ed — the consumer parks in the
  dispatch loop after close(), and leaving it pending leaks a task into the
  next event loop.

فارسی: پذیرش وب‌سوکت — دو مرورگرِ هم‌زمان، سقفِ اتصال با کدهای ۴۰۰۰/۴۰۰۳،
تازه‌سازیِ TTL و پُرکردنِ شکافِ اتصالِ دوباره؛ تست‌های hypertable فقط روی
پستگرس اجرا می‌شوند.
"""

import asyncio
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

import pytest
import redis as redis_lib
from channels.layers import InMemoryChannelLayer
from django.db import connection
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry import ws
from apps.telemetry.models import SaloonTelemetry
from apps.telemetry.ws_consumer import GAP_FILL_MAX_ROWS, SaloonTelemetryConsumer
from apps.tenants.models import Organization

from tests.conftest import INTEGRATION_REDIS_URL  # noqa: E402

PASSWORD = "strong-pass-123"
WS_PATH = "/ws/saloons/{saloon_id}/telemetry?token={token}"


def _run(coro):
    """One fresh event loop per scenario — the whole test lives inside it."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _spy_close(codes):
    """Record close codes while preserving the deny behavior."""
    original = SaloonTelemetryConsumer.close

    def spy(consumer_self, code=None):
        codes.append(code)
        return original(consumer_self, code=code)

    return mock.patch.object(SaloonTelemetryConsumer, "close", spy)


async def _expect_deny_close(communicator, code):
    """BUG-6 accept-then-close: connect() accepts, then the close frame carries
    the verdict code. Single source for the WS close contract."""
    connected, _ = await communicator.connect()
    assert connected is True, "deny path must accept() before close() (BUG-6)"
    message = await communicator.receive_output()
    assert message["type"] == "websocket.close", message
    assert message.get("code") == code, message
    return message


# ---------------------------------------------------------------------------
# World builder (M6 family convention)
# ---------------------------------------------------------------------------


def _make_world(*, users=("owner", "engineer", "worker")):
    tag = uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")

    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    saloon2 = Saloon.objects.create(org=org, farm=farm, name=f"s2-{tag}")

    # Real sensor slots — saloon_telemetry.slot_id is an FK to these.
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position
    )

    other_org = Organization.objects.create(name=f"other-{tag}")
    other_farm = Farm.objects.create(
        org=other_org, name=f"of-{tag}", province=province, city=city
    )
    other_saloon = Saloon.objects.create(org=other_org, farm=other_farm, name=f"os-{tag}")

    user_model = get_user_model()
    people = {}
    for role in users:
        user = user_model.objects.create_user(username=f"{role}-{tag}", password=PASSWORD)
        OrgMember.objects.create(user=user, org=org, role=role, is_active=True)
        people[role] = user
    stranger = user_model.objects.create_user(username=f"str-{tag}", password=PASSWORD)
    OrgMember.objects.create(user=stranger, org=other_org, role="owner", is_active=True)
    people["stranger"] = stranger

    return {
        "tag": tag, "org": org, "farm": farm,
        "saloon": saloon, "saloon2": saloon2, "other_saloon": other_saloon,
        "slot": slot, "people": people,
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


def _token_for(world, role):
    return _login(world["people"][role].username)


def _comm(world, token, saloon=None):
    """PRE-BUILT communicator — the token must already be minted (sync body
    only); connect()/disconnect() run inside the scenario's loop."""
    target = saloon or world["saloon"]
    communicator = WebsocketCommunicator(
        SaloonTelemetryConsumer.as_asgi(),
        WS_PATH.format(saloon_id=target.pk, token=token),
    )
    communicator.scope["url_route"] = {"kwargs": {"saloon_id": target.pk}}
    return communicator


def _cap_client():
    return redis_lib.Redis.from_url(INTEGRATION_REDIS_URL, decode_responses=True)


class _FakeLayers:
    """channel_layers singleton stand-in holding one InMemory layer."""

    def __init__(self, layer):
        self.backends = {"default": layer}

    def __getitem__(self, alias):
        return self.backends[alias]

    def __contains__(self, alias):
        return alias in self.backends


class _RecordingLayer:
    """Sync-capturing fake layer for the DRAIN (producer) test: group_send is
    a real coroutine (async_to_sync compatible) that records — no queues, no
    loop binding, observable from plain sync test code."""

    def __init__(self):
        self.sent = []

    async def group_send(self, group, message):
        self.sent.append((group, dict(message)))

    async def group_add(self, group, channel):
        pass

    async def group_discard(self, group, channel):
        pass


@pytest.fixture(autouse=True)
def _cascade_teardown():
    """Transactional teardown on PG needs the conftest cascade-flush patch:
    Django flushes TransactionTestCase WITHOUT CASCADE unless available_apps
    is set, and Postgres refuses to truncate an FK-referenced table even when
    the referencing (unmanaged) hypertable is empty. Idempotent; no-op on
    sqlite. (seeded_rls_db installs it for RLS tests — these tests run
    without that fixture.)"""
    if connection.vendor != "sqlite":
        from tests.conftest import _install_cascade_flush

        _install_cascade_flush(connection)
    yield


@pytest.fixture(autouse=True)
def _ws_stack(monkeypatch):
    """Isolated Redis (cap keys) + InMemory channel layer (group fan-out)."""
    client = redis_lib.Redis.from_url(INTEGRATION_REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.ws._redis_client", client)
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    layer = InMemoryChannelLayer(expiry=60.0)
    monkeypatch.setattr("channels.layers.channel_layers", _FakeLayers(layer))
    yield layer


# ---------------------------------------------------------------------------
# M6-06 — auth verdicts: 4003
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_missing_token_closes_4003(_ws_stack):
    world = _make_world()
    codes = []

    async def scenario():
        communicator = WebsocketCommunicator(
            SaloonTelemetryConsumer.as_asgi(),
            f"/ws/saloons/{world['saloon'].pk}/telemetry",
        )
        communicator.scope["url_route"] = {"kwargs": {"saloon_id": world["saloon"].pk}}
        with _spy_close(codes):
            await _expect_deny_close(communicator, 4003)
        await communicator.disconnect()  # let the parked app task finish

    _run(scenario())
    assert codes == [4003]


@pytest.mark.django_db(transaction=True)
def test_bad_token_closes_4003(_ws_stack):
    world = _make_world()
    codes = []
    communicator = _comm(world, "not-a-jwt")

    async def scenario():
        with _spy_close(codes):
            await _expect_deny_close(communicator, 4003)
        await communicator.disconnect()  # let the parked app task finish

    _run(scenario())
    assert codes == [4003]


@pytest.mark.django_db(transaction=True)
def test_cross_org_user_closes_4003(_ws_stack):
    world = _make_world()
    codes = []
    foreign = _comm(world, _token_for(world, "stranger"), saloon=world["saloon"])

    async def scenario():
        # A valid token for ANOTHER org — the claim's tenant context makes
        # the saloon invisible (fail-closed RLS), the same 4003 as missing.
        with _spy_close(codes):
            await _expect_deny_close(foreign, 4003)
        await foreign.disconnect()  # let the parked app task finish

    _run(scenario())
    assert codes == [4003]


# ---------------------------------------------------------------------------
# M6-06 — the cap: 4000
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_cap_closes_4000_then_frees(_ws_stack):
    world = _make_world()
    client = _cap_client()
    user = world["people"]["engineer"]
    cap = ws.config_int("ws_max_connections_per_user", 10)
    assert cap == 10  # seeded D21 value — the test means nothing otherwise
    codes = []
    full = _comm(world, _token_for(world, "engineer"))
    freed = _comm(world, _token_for(world, "engineer"))

    async def scenario():
        for index in range(cap):
            client.sadd(ws.active_set_key(user.pk), f"ghost-{index}")
        with _spy_close(codes):
            await _expect_deny_close(full, 4000)  # cap full: accept then close 4000
        assert codes == [4000]
        await full.disconnect()  # let the parked app task finish

        client.srem(ws.active_set_key(user.pk), "ghost-0")  # one slot frees
        connected2, _ = await freed.connect()
        assert connected2 is True
        hello = await freed.receive_json_from()
        assert hello["type"] == "telemetry.hello"
        await freed.disconnect()

    _run(scenario())


@pytest.mark.django_db(transaction=True)
def test_connect_registers_and_disconnect_cleans_active_set(_ws_stack):
    world = _make_world()
    client = _cap_client()
    user = world["people"]["worker"]
    key = ws.active_set_key(user.pk)
    communicator = _comm(world, _token_for(world, "worker"))

    async def scenario():
        connected, _ = await communicator.connect()
        assert connected is True
        await communicator.receive_json_from()  # hello

        assert client.scard(key) == 1
        assert client.ttl(key) > 0  # zombie guard armed on connect

        await communicator.disconnect()
        assert client.scard(key) == 0  # clean close releases the cap slot

    _run(scenario())


# ---------------------------------------------------------------------------
# M6-06 — two browsers see the same update (group fan-out)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_browsers_see_same_update(_ws_stack):
    world = _make_world()
    layer = _ws_stack
    owner_ws = _comm(world, _token_for(world, "owner"))
    worker_ws = _comm(world, _token_for(world, "worker"))

    async def scenario():
        assert (await owner_ws.connect())[0] is True
        assert (await worker_ws.connect())[0] is True

        hello_owner = await owner_ws.receive_json_from()
        hello_worker = await worker_ws.receive_json_from()
        assert hello_owner["type"] == "telemetry.hello"
        assert hello_owner["org_id"] == str(world["org"].org_id)
        assert hello_worker["org_id"] == hello_owner["org_id"]

        group = ws.group_name(str(world["org"].org_id), world["saloon"].pk)
        assert layer.groups.get(group), "both sockets joined the org-scoped group"

        # THE demo beat: one producer push, BOTH tabs get the same payload.
        message = ws.telemetry_update_message(
            saloon_id=world["saloon"].pk,
            slot_id="slot-x",
            device_ts="2026-09-06T10:00:00Z",
            metrics={"temp": {"avg": 28.4}},
        )
        await layer.group_send(group, message)
        seen_owner = await owner_ws.receive_json_from()
        seen_worker = await worker_ws.receive_json_from()
        assert seen_owner == seen_worker
        assert seen_owner["type"] == "telemetry.update"
        assert seen_owner["slot_id"] == "slot-x"
        assert seen_owner["metrics"] == {"temp": {"avg": 28.4}}

        await owner_ws.disconnect()
        await worker_ws.disconnect()

    _run(scenario())


@pytest.mark.django_db(transaction=True)
def test_push_lands_only_on_the_owning_saloon_group(_ws_stack):
    """Decision-19 boundary: the sibling saloon's socket stays silent."""
    world = _make_world()
    layer = _ws_stack
    other_ws = _comm(world, _token_for(world, "owner"), saloon=world["saloon2"])

    async def scenario():
        assert (await other_ws.connect())[0] is True
        await other_ws.receive_json_from()  # hello

        await layer.group_send(
            ws.group_name(str(world["org"].org_id), world["saloon"].pk),
            ws.telemetry_update_message(
                world["saloon"].pk, "slot-x", "2026-09-06T10:00:00Z", {"temp": {"avg": 28.4}}
            ),
        )
        # Silence probe WITHOUT receive-timeout: a timed-out receive_output
        # cancels the app task (asgiref treats it as a hung app), which would
        # poison the communicator — receive_nothing polls safely instead.
        assert await other_ws.receive_nothing(timeout=0.2)
        await other_ws.disconnect()

    _run(scenario())


# ---------------------------------------------------------------------------
# M6-06 — TTL refresh (the zombie guard)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_inbound_message_refreshes_ttl(_ws_stack):
    world = _make_world()
    client = _cap_client()
    user = world["people"]["engineer"]
    key = ws.active_set_key(user.pk)
    communicator = _comm(world, _token_for(world, "engineer"))

    async def scenario():
        assert (await communicator.connect())[0] is True
        await communicator.receive_json_from()  # hello

        client.expire(key, 240)  # pretend the guard decayed
        await communicator.send_json_to({"action": "ping"})  # unknown action: ignored
        await communicator.receive_nothing(timeout=0.2)  # no error came back
        assert client.ttl(key) > 290  # refreshed to the full guard window

        await communicator.disconnect()

    _run(scenario())


def test_register_drop_and_refresh_unit_paths(_ws_stack):
    client = _cap_client()
    connection_id = ws.register_connection(4242, "conn-1")
    assert connection_id == "conn-1"
    key = ws.active_set_key(4242)
    assert client.sismember(key, "conn-1")

    ws.refresh_connection_ttl(4242)
    assert client.ttl(key) > 290

    ws.drop_connection(4242, "conn-1")
    assert not client.sismember(key, "conn-1")
    ws.drop_connection(4242, "conn-1")  # idempotent — never an error

    # Redis-down posture: cap degrades open, helpers never raise.
    with mock.patch.object(ws, "ws_redis", side_effect=RuntimeError("down")):
        assert ws.active_count(4242) == 0
        ws.register_connection(4242, "conn-2")
        ws.refresh_connection_ttl(4242)
        ws.drop_connection(4242, "conn-2")


# ---------------------------------------------------------------------------
# M6-06 — the real producer: drain → post-commit push (PG only)
# ---------------------------------------------------------------------------


class _NoFlush(TransactionTestCase):
    """Integration base: real commits, zero framework truncation (m3_05)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.db import connection as dj_conn

        if dj_conn.vendor != "postgresql":
            raise unittest.SkipTest("drain truth requires PostgreSQL + Redis (CI/dev)")

    def _fixture_teardown(self):
        pass  # rows are isolated per-run by a fresh UUID org


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
class TestDrainPush(_NoFlush):
    """Publish → drain → COMMIT → group_send, on the real pipeline.

    House pattern verbatim from m3_05: transactional + use_migrations, and
    the shared dev streams start each test empty.
    """

    def setUp(self):
        from apps.telemetry.consumers import STREAM_HISTORICAL, STREAM_REALTIME

        tag = uuid.uuid4().hex[:8]
        self.tag = tag
        self.province = Province.objects.create(name=f"p-{tag}")
        self.city = City.objects.create(province=self.province, name=f"c-{tag}")
        self.org = Organization.objects.create(name=f"org-{tag}")
        self.farm = Farm.objects.create(
            org=self.org, name=f"f-{tag}", province=self.province, city=self.city
        )
        self.saloon = Saloon.objects.create(org=self.org, farm=self.farm, name=f"s-{tag}")
        position = SaloonPosition.objects.create(saloon=self.saloon, org=self.org, label="north")
        self.slot = SaloonSensorSlot.objects.create(
            slot_id=f"slot-{tag}", saloon=self.saloon, org=self.org, position=position
        )
        GatewaySensorMapping.objects.create(
            gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=self.slot, org=self.org
        )

        self.redis = _cap_client()
        # Streams/groups/dedup keys are shared dev resources: start clean.
        self.redis.delete(STREAM_REALTIME, STREAM_HISTORICAL)

        self.recorder = _RecordingLayer()
        patcher = mock.patch("apps.telemetry.ws.get_channel_layer", return_value=self.recorder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _publish(self, device_ts, buffered=False):
        from morghdari.subscriber.pipeline import handle_message

        # Topic's gateway segment and payload's mirrored gateway_slot_id must
        # AGREE (identity_fault → DLQ otherwise); the mapping's hardware id
        # must agree with gateway_hardware_id (else UNKNOWN_SLOT). m3_05 shape.
        gateway_slot = f"GW-{self.tag}"
        topic = (
            f"{self.org.org_id}/farms/{self.farm.pk}/gateways/{gateway_slot}/telemetry"
        )
        payload = json.dumps({
            "gateway_hardware_id": f"hw-{self.tag}",
            "gateway_slot_id": gateway_slot,
            "schema_version": "2.0",
            "device_ts": device_ts,
            "is_buffered": buffered,
            "readings": [{"pack_id": "P-A", "metrics": {"temp": {"avg": 28.4}}}],
        })
        handle_message(
            _cap_client(), topic, payload.encode(), db_fetch=lambda *a: self.slot.pk
        )

    def test_drain_commit_pushes_telemetry_update(self):
        from apps.telemetry.consumers import STREAM_REALTIME, drain_stream

        self._publish("2026-09-06T10:00:00Z")
        assert drain_stream(STREAM_REALTIME, count=10) >= 1
        group = ws.group_name(str(self.org.org_id), self.saloon.pk)
        assert (group, {
            "type": "telemetry.update",
            "saloon_id": self.saloon.pk,
            "slot_id": self.slot.slot_id,
            "device_ts": "2026-09-06T10:00:00Z",
            "is_buffered": False,
            "metrics": {"temp": {"avg": 28.4}},
        }) in self.recorder.sent

    def test_push_is_scoped_to_the_owning_saloon_group(self):
        from apps.telemetry.consumers import STREAM_REALTIME, drain_stream

        self._publish("2026-09-06T10:01:00Z")
        assert drain_stream(STREAM_REALTIME, count=10) >= 1
        pushed_groups = {group for group, _ in self.recorder.sent}
        assert pushed_groups == {ws.group_name(str(self.org.org_id), self.saloon.pk)}

    def test_redelivery_pushes_again_but_dedup_keeps_rows_once(self):
        """At-least-once on the wire, effectively-once in the table."""
        from apps.telemetry.consumers import STREAM_REALTIME, drain_stream

        self._publish("2026-09-06T10:05:00Z")
        self._publish("2026-09-06T10:05:00Z")  # same window — dedup guard drops
        assert drain_stream(STREAM_REALTIME, count=10) >= 1

        from apps.tenants.context import tenant_context

        with tenant_context(str(self.org.org_id)):
            rows = SaloonTelemetry.objects.filter(
                saloon_id=self.saloon.pk, slot_id=self.slot.slot_id,
                device_ts="2026-09-06T10:05:00Z",
            ).count()
        assert rows == 1  # the UNIQUE constraint absorbed the redelivery


def test_push_helper_skips_when_no_layer(_ws_stack):
    from apps.telemetry.ws import push_telemetry_updates

    with mock.patch("apps.telemetry.ws.get_channel_layer", return_value=None):
        assert (
            push_telemetry_updates(
                "org", [{"saloon_id": 1, "device_ts": "t", "metrics_by_slot": {"s": {}}}]
            )
            == 0
        )


# ---------------------------------------------------------------------------
# M6-07 — reconnect gap-fill (hypertable reads → PG only)
# ---------------------------------------------------------------------------


def _seed_gap_rows(world, rows):
    """Committed telemetry rows the reconnecting client missed."""
    base = datetime(2026, 9, 6, 10, 0, tzinfo=dt_timezone.utc)
    SaloonTelemetry.objects.bulk_create([
        SaloonTelemetry(
            server_received_ts=base + timedelta(minutes=100 + index),
            device_ts=base + timedelta(seconds=offset_seconds),
            org=world["org"],
            saloon=world["saloon"],
            slot=slot,
            is_buffered=False,
            metrics={"temp": {"avg": 20.0 + offset_seconds}},
        )
        for index, (offset_seconds, slot) in enumerate(rows)
    ])


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_gap_fill_answers_ordered_rows(timescale_db):
    if connection.vendor != "postgresql":
        pytest.skip("saloon_telemetry is a TimescaleDB hypertable")
    world = _make_world()
    # Two rows inserted OUT of device_ts order — the reply must re-sort.
    _seed_gap_rows(world, [(120, world["slot"]), (60, world["slot"])])
    communicator = _comm(world, _token_for(world, "engineer"))

    async def scenario():
        assert (await communicator.connect())[0] is True
        await communicator.receive_json_from()  # hello

        await communicator.send_json_to({"action": "gap_fill", "since": "2026-09-06T09:59:00Z"})
        reply = await communicator.receive_json_from()
        assert reply["type"] == "telemetry.gap_fill"
        assert reply["saloon_id"] == world["saloon"].pk
        stamps = [row["device_ts"] for row in reply["rows"]]
        assert stamps == sorted(stamps)
        assert len(reply["rows"]) == 2
        assert reply["rows"][0]["metrics"] == {"temp": {"avg": 80.0}}  # 60 s row first
        assert reply["rows"][1]["metrics"] == {"temp": {"avg": 140.0}}  # then 120 s
        assert reply["rows"][0]["slot_id"] == world["slot"].slot_id
        assert reply["rows"][0]["is_buffered"] is False

        await communicator.disconnect()

    _run(scenario())


@pytest.mark.django_db(transaction=True)
def test_gap_fill_bad_since_is_friendly(_ws_stack):
    """Malformed/missing cursor → explicit envelope, empty rows, no raise.
    (No hypertable read happens — safe on any backend.)"""
    world = _make_world()
    communicator = _comm(world, _token_for(world, "worker"))

    async def scenario():
        assert (await communicator.connect())[0] is True
        await communicator.receive_json_from()  # hello

        await communicator.send_json_to({"action": "gap_fill", "since": "not-a-date"})
        reply = await communicator.receive_json_from()
        assert reply["type"] == "telemetry.gap_fill"
        assert reply["rows"] == []
        assert "since" in reply["error"]

        await communicator.send_json_to({"action": "gap_fill"})
        reply = await communicator.receive_json_from()
        assert reply["rows"] == []

        await communicator.send_json_to({"action": "mystery"})  # ignored safely
        await communicator.receive_nothing(timeout=0.2)

        await communicator.disconnect()

    _run(scenario())


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_gap_fill_slot_filter_and_ttl_refresh(timescale_db):
    if connection.vendor != "postgresql":
        pytest.skip("saloon_telemetry is a TimescaleDB hypertable")
    world = _make_world()
    client = _cap_client()
    user = world["people"]["owner"]
    key = ws.active_set_key(user.pk)

    position_b = SaloonPosition.objects.create(
        saloon=world["saloon"], org=world["org"], label="south"
    )
    slot_b = SaloonSensorSlot.objects.create(
        slot_id=f"slotb-{world['tag']}", saloon=world["saloon"], org=world["org"],
        position=position_b,
    )
    _seed_gap_rows(world, [(120, world["slot"]), (60, slot_b)])
    communicator = _comm(world, _token_for(world, "owner"))

    async def scenario():
        assert (await communicator.connect())[0] is True
        await communicator.receive_json_from()  # hello

        client.expire(key, 200)
        await communicator.send_json_to(
            {"action": "gap_fill", "since": "2026-09-06T09:00:00Z",
             "slot_id": slot_b.slot_id}
        )
        reply = await communicator.receive_json_from()
        assert [row["slot_id"] for row in reply["rows"]] == [slot_b.slot_id]
        assert client.ttl(key) > 290  # the fill is a live message: TTL refreshed

        await communicator.disconnect()

    _run(scenario())


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_gap_fill_respects_row_ceiling(timescale_db):
    if connection.vendor != "postgresql":
        pytest.skip("saloon_telemetry is a TimescaleDB hypertable")
    world = _make_world()
    _seed_gap_rows(
        world, [(index, world["slot"]) for index in range(GAP_FILL_MAX_ROWS + 25)]
    )
    communicator = _comm(world, _token_for(world, "owner"))

    async def scenario():
        assert (await communicator.connect())[0] is True
        await communicator.receive_json_from()  # hello

        await communicator.send_json_to({"action": "gap_fill", "since": "2026-09-06T00:00:00Z"})
        reply = await communicator.receive_json_from()
        assert len(reply["rows"]) == GAP_FILL_MAX_ROWS  # bounded, never unbounded

        await communicator.disconnect()

    _run(scenario())


@pytest.mark.django_db(transaction=True)
def test_compat_ws_farmSlug_spelling_does_not_raise_keyerror(_ws_stack):
    """BUG-7 (QA 15.7): compat route passes farmSlug but consumer read farm_slug.

    Connect-level: the real route kwarg arrives as farmSlug; the consumer must
    accept it (canonical stays farm_slug) instead of raising KeyError.
    """
    from apps.compat.consumers_compat import FarmTelemetryConsumer

    world = _make_world()
    token = _token_for(world, "owner")
    codes = []
    original = FarmTelemetryConsumer.close

    def spy(consumer_self, code=None):
        codes.append(code)
        return original(consumer_self, code=code)

    communicator = WebsocketCommunicator(
        FarmTelemetryConsumer.as_asgi(),
        f"/ws/farms/no-such-farm/telemetry?token={token}",
    )
    # Real route spelling (urls_compat FARM group): farmSlug, not farm_slug.
    communicator.scope["url_route"] = {"kwargs": {"farmSlug": "no-such-farm"}}

    async def scenario():
        with mock.patch.object(FarmTelemetryConsumer, "close", spy):
            with mock.patch(
                "apps.compat.consumers_compat.svc.compat_resolve_farm",
                return_value=(None, None),
            ):
                connected, _ = await communicator.connect()
                assert connected is True  # BUG-6: accept() before close()
                message = await communicator.receive_output()
                assert message["type"] == "websocket.close" and message.get("code") == 4003
        await communicator.disconnect()

    _run(scenario())
    assert codes == [4003]
