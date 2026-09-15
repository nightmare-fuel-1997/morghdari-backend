"""BUG-8 (QA 15.8) RED: WS connect must fail-open when Redis is down.

Valid token + redis raising ConnectionError during connect → accept/auth-path,
never 500/close. Auth denies must still be enforced (deny path untouched).
"""
import asyncio
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
    """Mirror test_m6_06_ws: transactional teardown needs CASCADE flush on PG."""
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


@pytest.mark.django_db(transaction=True)
def test_redis_down_valid_token_still_accepts(_ws_isolated):
    """RED for BUG-8: redis down must not 500 a valid-token connect."""
    world = _make_world()
    communicator = _comm(world, _token_for(world, "owner"))
    codes = []

    async def scenario():
        with mock.patch(
            "apps.telemetry.ws.ws_redis", side_effect=ConnectionError("redis down")
        ):
            from channels.layers import get_channel_layer

            layer = get_channel_layer()

            async def failing_add(group, channel):
                raise ConnectionError("channel redis down")

            with mock.patch.object(layer, "group_add", failing_add):
                with _spy_close(codes):
                    connected, _ = await communicator.connect()
                assert connected is True
                hello = await communicator.receive_json_from()
                assert hello["type"] == "telemetry.hello"
                await communicator.disconnect()

    _run(scenario())
    assert codes == []


@pytest.mark.django_db(transaction=True)
def test_redis_down_bad_token_still_denied_4003(_ws_isolated):
    """Guard: fail-open must not open auth — bad token still 4003, even redis-down."""
    world = _make_world()
    communicator = _comm(world, "not-a-jwt")
    codes = []

    async def scenario():
        with mock.patch(
            "apps.telemetry.ws.ws_redis", side_effect=ConnectionError("redis down")
        ):
            with _spy_close(codes):
                connected, _ = await communicator.connect()
            assert connected is False
            await communicator.disconnect()

    _run(scenario())
    assert codes == [4003]
