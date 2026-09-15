"""BUG-8 live-hang RED: a HANGING channel layer must not hold the WS handshake.

Live (redis container stopped) the TCP connect blackholes: group_add neither
returns nor raises for many seconds, and Daphne severs the handshake at ~5s
(nginx 502). The consumer must bound the join by wall-clock and still
accept + greet. (Sync-redis hangs are converted to fast raises by the
socket-timeout knobs; this test pins the code-level wall-clock bound.)
"""
import time
from unittest import mock

import pytest
import redis as redis_lib
from channels.layers import InMemoryChannelLayer
from django.db import connection

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
def test_hanging_group_join_still_accepts_quickly(_ws_isolated):
    """RED for BUG-8 (live shape): group_add that hangs must not hold connect."""
    world = _make_world()
    communicator = _comm(world, _token_for(world, "owner"))
    codes = []

    async def scenario():
        from channels.layers import get_channel_layer

        layer = get_channel_layer()

        async def hanging_add(group, channel):
            import asyncio as _aio

            await _aio.sleep(20)

        with mock.patch.object(layer, "group_add", hanging_add):
            with _spy_close(codes):
                # Generous harness timeout — the block bound under test is
                # 2.5s; the wall clock below is the real assertion.
                connected, _ = await communicator.connect(timeout=10)
            assert connected is True
            # The join is bounded at ~2s by wall-clock; hello follows even
            # though the join itself hangs 20s. Generous receive timeout —
            # the assertion that matters is the wall clock below.
            hello = await communicator.receive_json_from(timeout=10)
            assert hello["type"] == "telemetry.hello"
            await communicator.disconnect()

    t0 = time.monotonic()
    _run(scenario())
    wall = time.monotonic() - t0
    assert wall < 12, f"handshake held {wall:.1f}s by a hanging group-join"
    assert codes == []
