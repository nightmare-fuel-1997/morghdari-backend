"""BUG-8 residual (QA 15.8 note): rejoin-fallback after a degraded hello.

A socket that connected while Redis was down gets a degraded hello and never
joins its saloon group — when Redis returns, the tab stays silent until the
client reconnects. The consumer must late-join on the next inbound message
(every inbound message is already a liveness proof) and greet again, with a
cooldown so a still-dead layer is not hammered per message.
"""
from unittest import mock

import pytest
import redis as redis_lib
from channels.layers import InMemoryChannelLayer
from django.db import connection

from apps.telemetry.ws_consumer import SaloonTelemetryConsumer
from tests.conftest import INTEGRATION_REDIS_URL
from tests.test_m6_06_ws import (
    _comm,
    _make_world,
    _run,
    _spy_close,
    _token_for,
    _FakeLayers,
)


@pytest.fixture(autouse=True)
def _cascade_teardown():
    if connection.vendor != "sqlite":
        from tests.conftest import _install_cascade_flush

        _install_cascade_flush(connection)
    yield


@pytest.fixture
def _ws_isolated(monkeypatch):
    client = redis_lib.Redis.from_url(INTEGRATION_REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.telemetry.ws._redis_client", client)
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    layer = InMemoryChannelLayer(expiry=60.0)
    monkeypatch.setattr("channels.layers.channel_layers", _FakeLayers(layer))
    return layer


async def _failing_add(group, channel):
    raise ConnectionError("channel redis down")


@pytest.mark.django_db(transaction=True)
def test_degraded_socket_rejoins_on_next_inbound(_ws_isolated):
    """Degraded hello → redis back → next inbound message rejoins + regreets."""
    layer = _ws_isolated
    world = _make_world()
    communicator = _comm(world, _token_for(world, "owner"))
    codes = []
    joins = []

    async def scenario():
        async def recording_add(group, channel):
            joins.append(group)
            return await InMemoryChannelLayer.group_add(layer, group, channel)

        with mock.patch(
            "apps.telemetry.ws.ws_redis", side_effect=ConnectionError("redis down")
        ):
            with mock.patch.object(layer, "group_add", _failing_add):
                with _spy_close(codes):
                    connected, _ = await communicator.connect()
                assert connected is True
                hello = await communicator.receive_json_from()
                assert hello["type"] == "telemetry.hello"
        # Redis is back: the next inbound message must late-join and regreet.
        with mock.patch.object(layer, "group_add", recording_add):
            await communicator.send_json_to({})
            regreet = await communicator.receive_json_from()
            assert regreet["type"] == "telemetry.rejoined"
            assert regreet["saloon_id"] == world["saloon"].pk
        # A further message must NOT rejoin again (already joined).
        with mock.patch.object(layer, "group_add", recording_add):
            await communicator.send_json_to({})
        await communicator.disconnect()

    _run(scenario())
    assert codes == []
    assert len(joins) == 1
    assert joins[0].startswith("saloon_")


@pytest.mark.django_db(transaction=True)
def test_healthy_socket_never_rejoins(_ws_isolated):
    """A socket that joined at connect must not rejoin or regreet on traffic."""
    layer = _ws_isolated
    world = _make_world()
    communicator = _comm(world, _token_for(world, "owner"))
    codes = []
    joins = []

    async def scenario():
        async def recording_add(group, channel):
            joins.append(group)
            return await InMemoryChannelLayer.group_add(layer, group, channel)

        with mock.patch.object(layer, "group_add", recording_add):
            with _spy_close(codes):
                connected, _ = await communicator.connect()
            assert connected is True
            hello = await communicator.receive_json_from()
            assert hello["type"] == "telemetry.hello"
            n_joins_at_hello = len(joins)
            assert n_joins_at_hello == 1
            await communicator.send_json_to({})
            # Only the hello is ever sent — no telemetry.rejoined follows.
            await communicator.receive_nothing(timeout=1)
        await communicator.disconnect()

    _run(scenario())
    assert codes == []
    assert len(joins) == 1


@pytest.mark.django_db(transaction=True)
def test_rejoin_attempt_while_layer_down_never_breaks_socket(_ws_isolated):
    """Inbound messages while the layer is still dead: no raise, no close."""
    layer = _ws_isolated
    world = _make_world()
    communicator = _comm(world, _token_for(world, "owner"))
    codes = []
    attempts = []

    async def counting_add(group, channel):
        attempts.append(group)
        raise ConnectionError("channel redis still down")

    async def scenario():
        with mock.patch(
            "apps.telemetry.ws.ws_redis", side_effect=ConnectionError("redis down")
        ):
            with mock.patch.object(layer, "group_add", _failing_add):
                with _spy_close(codes):
                    connected, _ = await communicator.connect()
                assert connected is True
                await communicator.receive_json_from()  # degraded hello
        # Layer still dead (ws_redis back for TTL, group_add failing).
        # receive_nothing synchronizes: the app must consume the queued
        # inbound messages before teardown, else disconnect wins the race
        # and the attempts never run.
        with mock.patch.object(layer, "group_add", counting_add):
            await communicator.send_json_to({})
            await communicator.send_json_to({})
            await communicator.receive_nothing(timeout=1)
        await communicator.disconnect()

    _run(scenario())
    assert codes == []
    # Cooldown: rapid-fire messages collapse into a single attempt.
    assert len(attempts) == 1
