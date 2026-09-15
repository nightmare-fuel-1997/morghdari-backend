"""BUG-22 regression (QA 15.9) — the channel layer must be allowed to long-poll.

The BUG-8 fix bounded the async layer with socket_timeout=1 — but channels_redis
receive() is a 5 s BRPOP loop (core.py brpop_timeout=5): a 1 s redis-py read
timeout raises into the ASGI app and Daphne closes every otherwise-idle WS
~1-2.5 s after connect (live: WSCONNECT→WSDISCONNECT 2 s, sockets died with
redis UP). The connect knob stays (DNS/TCP blackhole was the real BUG-8 hang);
the read knob must NOT exist on the layer.

فارسی: لایهٔ کانال باید بتواند با BRPOP پنج‌ثانیه‌ای منتظر بماند؛ سقفِ خواندن
روی همان لایه، اتصال‌های بی‌کار را می‌کُشد — فقط مکثِ «اتصال» محدود می‌ماند.
"""

from django.conf import settings


def test_channel_layer_connect_bounded_but_read_unbounded():
    host = settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"][0]
    # BUG-8 invariant kept: connect/DNS blackhole stays bounded…
    assert host.get("socket_connect_timeout") is not None and host["socket_connect_timeout"] <= 2
    # BUG-22 invariant NEW: no socket_timeout on the layer — receive() is a
    # brpop_timeout=5 long-poll; a shorter read timeout kills idle sockets.
    assert "socket_timeout" not in host, (
        "channel layer carries socket_timeout — channels_redis receive() "
        "long-polls (brpop_timeout=5) and a short read timeout closes every "
        "idle WS seconds after connect (QA 15.9 live regression)"
    )
