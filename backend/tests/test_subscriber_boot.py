"""Subscriber boot resilience — retry beat and CONNACK honesty (M3-03).

The bridge must survive a broker that is down at boot: main() retries the
FIRST connect on a fixed 2s beat (paho's own reconnect only kicks in after a
successful first connect), and a CONNACK rejection must be logged loudly,
never silently. All MQTT I/O is mocked — these tests pin the retry/backoff/
logging behavior, not the broker.

فارسی: پایداریِ بوتِ مشترک — قطعیِ اولیهٔ بروکر نباید بریج را بکشد؛ ردِ
CONNACK باید بلند و قابلِ رؤیت ثبت شود، نه بی‌صدا.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from morghdari.subscriber import main as sub


@pytest.fixture
def fake_client(monkeypatch):
    """A mocked paho Client installed where build_client() looks for it,
    plus the stable client id the bridge refuses to boot without."""
    client = MagicMock()
    monkeypatch.setattr(sub.mqtt, "Client", lambda *a, **k: client)
    monkeypatch.setenv("SUBSCRIBER_CLIENT_ID", "test-subscriber")
    return client


def test_main_retries_first_connect_on_fixed_beat_then_loops(fake_client, monkeypatch, caplog):
    """Broker down at boot: connect raises OSError once, main waits exactly
    2s (the documented beat), retries, and only then enters loop_forever —
    a boot-time outage must never kill the bridge."""
    fake_client.connect.side_effect = [OSError("connection refused"), None]
    monkeypatch.setenv("MQTT_HOST", "broker.test")
    monkeypatch.setenv("MQTT_PORT", "1884")
    sleeps = []
    monkeypatch.setattr(sub.time, "sleep", sleeps.append)

    with caplog.at_level("WARNING", logger="morghdari.subscriber"):
        sub.main()

    assert fake_client.connect.call_count == 2
    assert fake_client.connect.call_args == (("broker.test", 1884), {"keepalive": 60})
    assert sleeps == [2]  # exactly one 2s beat between the failed and the retry
    assert "broker unreachable (connection refused); retrying in 2s" in caplog.text
    assert fake_client.loop_forever.called


def test_main_connects_immediately_when_broker_is_up(fake_client, monkeypatch):
    """No outage → no retry beat at all: connect once, straight to the loop."""
    monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("MQTT_PORT", "1883")
    sleeps = []
    monkeypatch.setattr(sub.time, "sleep", sleeps.append)

    sub.main()

    assert fake_client.connect.call_count == 1
    assert sleeps == []
    assert fake_client.loop_forever.called


def test_on_connect_logs_conack_failure_and_skips_subscribe(fake_client, caplog):
    """A CONNACK rejection is the loudest possible failure: logged at ERROR
    and the bridge does NOT subscribe (a subscription would silently queue
    nothing against a session the broker rejected)."""
    class _Failure:
        is_failure = True

    with caplog.at_level("ERROR", logger="morghdari.subscriber"):
        sub._on_connect(None, None, None, _Failure())

    assert "CONNACK failure" in caplog.text
    fake_client.subscribe.assert_not_called()


def test_on_connect_subscribes_telemetry_and_status_on_success(fake_client, caplog):
    """A successful CONNACK requests ALL THREE lanes — the telemetry share
    group, the pack-liveness status topic, and (Phase 3 / task 3.1) the
    command-response lane — and logs the session flag. The "subscribed"
    confirmation only lands when the broker's SUBACK grants (see the SUBACK
    tests below): a queued SUBSCRIBE is not a subscription."""
    fake_client.subscribe.return_value = (0, 321)  # MQTT_ERR_SUCCESS == 0

    class _Ok:
        is_failure = False

    with caplog.at_level("INFO", logger="morghdari.subscriber"):
        sub._on_connect(fake_client, None, SimpleNamespace(session_present=False), _Ok())

    assert "connected to broker (session_present=False)" in caplog.text
    topics = [c.args[0] for c in fake_client.subscribe.call_args_list]
    assert topics == [sub.SHARED_TOPIC, sub.STATUS_TOPIC, sub.COMMAND_RESPONSE_TOPIC]
    assert "subscribed via" not in caplog.text  # no SUBACK yet — no claim yet


@pytest.fixture
def clean_sub_state():
    """The SUBACK bookkeeping is module-global — each test starts blank."""
    sub._pending_subs.clear()
    sub._granted_topics.clear()
    yield
    sub._pending_subs.clear()
    sub._granted_topics.clear()


def test_on_subscribe_granted_logs_the_lane(fake_client, clean_sub_state, caplog):
    """A granted SUBACK is the ONLY "subscribed" log that means the broker
    holds the subscription — one per lane, naming the grant."""
    sub._pending_subs[11] = sub.SHARED_TOPIC
    sub._pending_subs[12] = sub.STATUS_TOPIC
    sub._pending_subs[13] = sub.COMMAND_RESPONSE_TOPIC

    with caplog.at_level("INFO", logger="morghdari.subscriber"):
        sub._on_subscribe(fake_client, None, 11, [1])
        sub._on_subscribe(fake_client, None, 12, [1])
        sub._on_subscribe(fake_client, None, 13, [1])

    assert "subscribed via" in caplog.text
    assert "subscribed to +/farms/+/gateways/+/status" in caplog.text
    assert "subscribed to +/farms/+/gateways/+/commands/response" in caplog.text
    assert sub._subscriptions_ok()


def test_on_subscribe_denied_is_critical_and_resubscribes(
    fake_client, clean_sub_state, monkeypatch, caplog
):
    """M7-10 live-drill regression: a denied SUBACK (0x80, e.g. EMQX still
    initializing authz at boot) must be CRITICAL-loud and resubscribe with
    backoff — never the old silent "subscribed" lie that left ingestion dead
    with zero broker-side subscriptions."""
    timers = []

    class _Timer:
        def __init__(self, delay, fn, args=None, kwargs=None):
            timers.append((delay, fn, args))

        daemon = False

        def start(self):
            pass

    monkeypatch.setattr(sub.threading, "Timer", _Timer)
    sub._pending_subs[7] = sub.SHARED_TOPIC
    fake_client.subscribe.return_value = (0, 44)  # retry re-sends cleanly

    with caplog.at_level("INFO", logger="morghdari.subscriber"):
        sub._on_subscribe(fake_client, None, 7, [0x80])

    assert "SUBACK DENIED" in caplog.text
    assert sub.SHARED_TOPIC in caplog.text
    assert not sub._subscriptions_ok()
    assert timers and timers[0][0] == sub._SUBSCRIBE_RETRY_S
    # firing the backoff re-sends both subscribes:
    timers[0][1](*timers[0][2])
    topics = [c.args[0] for c in fake_client.subscribe.call_args_list]
    assert topics == [sub.SHARED_TOPIC, sub.STATUS_TOPIC, sub.COMMAND_RESPONSE_TOPIC]


def test_build_client_uses_the_required_stable_client_id(fake_client, monkeypatch):
    """The persistent session is bound to the configured client id — the
    bridge must refuse to boot without it (a guessed id would orphan the
    queued session), and must pass it through verbatim with clean_session
    False so the broker queues offline messages against the right identity."""
    captured = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr(sub.mqtt, "Client", _capture)

    monkeypatch.delenv("SUBSCRIBER_CLIENT_ID", raising=False)  # fixture set it
    with pytest.raises(SystemExit):
        sub.build_client()

    monkeypatch.setenv("SUBSCRIBER_CLIENT_ID", "demo-subscriber-01")
    sub.build_client()
    assert captured["client_id"] == "demo-subscriber-01"
    assert captured["clean_session"] is False
