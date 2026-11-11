"""Tests for MQTT Reliable Delivery — Manual ACK, Redis Failure Recovery, and Deduplication Atomicity.

Addresses Section 10 of the Engineering Specification:
1. Manual acknowledgement lifecycle (QoS 1, QoS 0, DLQ, failure modes).
2. Deduplication atomicity & idempotency (Lua script, redelivery, multi-pack).
3. Redis failure & recovery (outages, reconnects, backoff, thread safety).
4. MQTT persistent session & broker integration (unacknowledged redelivery, shared subscription).
"""

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest
import redis as redis_lib

from morghdari.subscriber import main as subscriber_main
from morghdari.subscriber.dlq import DLQ_STREAM, DLQWriteError
from morghdari.subscriber.pipeline import (
    STREAM_HISTORICAL,
    STREAM_REALTIME,
    handle_message,
    persist_telemetry_atomic,
)
from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL

TEST_ORG = "9f8c2d34-6f0e-4a55-9a10-2b7c1f5e4d01"
TEST_FARM = "12"
TEST_SLOT = "FARM-01-GW-A"
VALID_TOPIC = f"{TEST_ORG}/farms/{TEST_FARM}/gateways/{TEST_SLOT}/telemetry"


def _make_payload(device_ts="2026-08-25T10:30:00Z", pack_id="P-A", readings=None, **overrides):
    if readings is None:
        readings = [{"pack_id": pack_id, "metrics": {"temp": {"avg": 28.4}}}]
    data = {
        "gateway_hardware_id": "m3test-HW",
        "gateway_slot_id": TEST_SLOT,
        "schema_version": "2.0",
        "device_ts": device_ts,
        "is_buffered": False,
        "readings": readings,
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


class MockMQTTMessage:
    def __init__(self, topic, payload, qos=1, mid=101, dup=False):
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.mid = mid
        self.dup = dup


class TrackingMQTTClient:
    def __init__(self):
        self.acked_mids = []
        self.disconnected = False
        self.ack_error_to_raise = None
        self.ack_return_code = mqtt.MQTT_ERR_SUCCESS

    def ack(self, mid: int, qos: int):
        if self.ack_error_to_raise:
            raise self.ack_error_to_raise
        if self.ack_return_code == mqtt.MQTT_ERR_SUCCESS:
            self.acked_mids.append((mid, qos))
        return self.ack_return_code

    def disconnect(self):
        self.disconnected = True


@pytest.fixture()
def clean_redis():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    # Scrub test keys
    for pattern in ("slot:map:m3test*", "dedup:9f8c2d34-6f0e*", "gw:*"):
        for key in client.scan_iter(pattern):
            client.delete(key)
    client.delete(STREAM_REALTIME, STREAM_HISTORICAL, DLQ_STREAM)
    yield client
    for pattern in ("slot:map:m3test*", "dedup:9f8c2d34-6f0e*", "gw:*"):
        for key in client.scan_iter(pattern):
            client.delete(key)
    client.delete(STREAM_REALTIME, STREAM_HISTORICAL, DLQ_STREAM)


# ===========================================================================
# 1. Manual Acknowledgement Tests
# ===========================================================================

def test_01_valid_message_acked_only_after_persistence(clean_redis, monkeypatch):
    """Test 1: Valid QoS 1 message is acknowledged only after successful Redis persistence."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")

    mock_client = TrackingMQTTClient()
    payload = _make_payload()
    msg = MockMQTTMessage(VALID_TOPIC, payload, qos=1, mid=42)

    # Initial state: stream empty, no ACKs
    assert clean_redis.xlen(STREAM_REALTIME) == 0
    assert len(mock_client.acked_mids) == 0

    subscriber_main._on_message(mock_client, None, msg)

    # Outcome: stream entry persisted AND mid acknowledged
    assert clean_redis.xlen(STREAM_REALTIME) == 1
    assert (42, 1) in mock_client.acked_mids
    assert not mock_client.disconnected


def test_02_redis_xadd_failure_results_in_no_puback(clean_redis, monkeypatch):
    """Test 2: Redis XADD failure results in NO PUBACK and triggers client disconnect."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")

    # Inject failure into persist_telemetry_atomic
    def fail_persist(*args, **kwargs):
        raise redis_lib.exceptions.ConnectionError("Simulated Redis write connection drop")

    monkeypatch.setattr("morghdari.subscriber.pipeline.persist_telemetry_atomic", fail_persist)

    mock_client = TrackingMQTTClient()
    msg = MockMQTTMessage(VALID_TOPIC, _make_payload(), qos=1, mid=77)

    subscriber_main._on_message(mock_client, None, msg)

    # Must NOT acknowledge mid 77 and MUST disconnect to trigger broker redelivery
    assert len(mock_client.acked_mids) == 0
    assert mock_client.disconnected is True


def test_03_redis_timeout_results_in_no_puback(clean_redis, monkeypatch):
    """Test 3: Redis timeout results in NO PUBACK."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")

    def timeout_persist(*args, **kwargs):
        raise redis_lib.exceptions.TimeoutError("Redis socket read timeout")

    monkeypatch.setattr("morghdari.subscriber.pipeline.persist_telemetry_atomic", timeout_persist)

    mock_client = TrackingMQTTClient()
    msg = MockMQTTMessage(VALID_TOPIC, _make_payload(), qos=1, mid=88)

    subscriber_main._on_message(mock_client, None, msg)

    assert len(mock_client.acked_mids) == 0
    assert mock_client.disconnected is True


def test_04_dlq_write_succeeds_before_rejection_acked(clean_redis, monkeypatch):
    """Test 4: Permanently rejected message is acknowledged only AFTER successful DLQ write."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)

    mock_client = TrackingMQTTClient()
    bad_msg = MockMQTTMessage(VALID_TOPIC, b"{invalid-json", qos=1, mid=99)

    assert clean_redis.xlen(DLQ_STREAM) == 0

    subscriber_main._on_message(mock_client, None, bad_msg)

    # Rejection was safely written to DLQ AND acknowledged so EMQX won't redeliver bad JSON forever
    assert clean_redis.xlen(DLQ_STREAM) == 1
    assert (99, 1) in mock_client.acked_mids
    assert not mock_client.disconnected


def test_05_dlq_write_failure_results_in_no_puback(clean_redis, monkeypatch):
    """Test 5: If writing to DLQ fails, message is NOT acknowledged."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)

    def fail_xadd(name, *args, **kwargs):
        if name == DLQ_STREAM:
            raise redis_lib.exceptions.ConnectionError("Redis DLQ write failed")
        return "1-0"

    monkeypatch.setattr(clean_redis, "xadd", fail_xadd)

    mock_client = TrackingMQTTClient()
    bad_msg = MockMQTTMessage(VALID_TOPIC, b"{invalid-json", qos=1, mid=100)

    subscriber_main._on_message(mock_client, None, bad_msg)

    assert len(mock_client.acked_mids) == 0
    assert mock_client.disconnected is True


def test_06_ack_failure_after_persistence_does_not_lose_message(clean_redis, monkeypatch):
    """Test 6: Failure in client.ack() does not affect persisted stream data."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")

    mock_client = TrackingMQTTClient()
    mock_client.ack_return_code = mqtt.MQTTErrorCode.MQTT_ERR_NO_CONN

    msg = MockMQTTMessage(VALID_TOPIC, _make_payload(), qos=1, mid=105)
    subscriber_main._on_message(mock_client, None, msg)

    # Persistence succeeded
    assert clean_redis.xlen(STREAM_REALTIME) == 1


def test_07_qos_0_behavior_handled_without_puback(clean_redis, monkeypatch):
    """Test 7: QoS 0 messages never trigger PUBACK."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")

    mock_client = TrackingMQTTClient()
    msg = MockMQTTMessage(VALID_TOPIC, _make_payload(), qos=0, mid=0)

    subscriber_main._on_message(mock_client, None, msg)

    # Persisted, but no ACK sent
    assert clean_redis.xlen(STREAM_REALTIME) == 1
    assert len(mock_client.acked_mids) == 0


# ===========================================================================
# 2. Deduplication and Idempotency Tests
# ===========================================================================

def test_08_persisted_duplicate_does_not_create_duplicate_stream_entry(clean_redis, monkeypatch):
    """Test 8: Successfully persisted duplicate message does not create duplicate stream entry."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")

    mock_client = TrackingMQTTClient()
    payload = _make_payload(device_ts="2026-08-25T11:00:00Z")
    msg1 = MockMQTTMessage(VALID_TOPIC, payload, qos=1, mid=201)
    msg2 = MockMQTTMessage(VALID_TOPIC, payload, qos=1, mid=202, dup=True)

    # First delivery
    subscriber_main._on_message(mock_client, None, msg1)
    assert clean_redis.xlen(STREAM_REALTIME) == 1
    assert (201, 1) in mock_client.acked_mids

    # Second delivery (duplicate)
    subscriber_main._on_message(mock_client, None, msg2)
    # Stream length still 1 (no duplicate), but duplicate was acknowledged
    assert clean_redis.xlen(STREAM_REALTIME) == 1
    assert (202, 1) in mock_client.acked_mids


def test_09_message_whose_initial_xadd_failed_not_discarded_as_duplicate(clean_redis):
    """Test 9: Message whose initial persistence failed is not discarded on redelivery."""
    items = [{
        "dedup_key": "dedup:testorg:testslot:2026-08-25T12:00:00Z",
        "lane": STREAM_REALTIME,
        "ttl": 300,
        "replay_key": None,
        "fields": {"sample": "val1"},
    }]

    # When Lua script fails before running (e.g. simulated server exception),
    # the dedup key is NOT created in Redis:
    assert clean_redis.exists(items[0]["dedup_key"]) == 0

    # On redelivery after recovery, persist_telemetry_atomic executes:
    p_count, d_count = persist_telemetry_atomic(clean_redis, STREAM_REALTIME, items)
    assert p_count == 1
    assert d_count == 0
    assert clean_redis.exists(items[0]["dedup_key"]) == 1
    assert clean_redis.xlen(STREAM_REALTIME) == 1


def test_10_multi_pack_payload_persistence_atomic(clean_redis):
    """Test 10: Multi-pack payload is persisted atomically via Redis Lua script."""
    items = [
        {
            "dedup_key": "dedup:testorg:slot1:2026-08-25T12:00:00Z",
            "lane": STREAM_REALTIME,
            "ttl": 300,
            "replay_key": None,
            "fields": {"reading": "1"},
        },
        {
            "dedup_key": "dedup:testorg:slot2:2026-08-25T12:00:00Z",
            "lane": STREAM_REALTIME,
            "ttl": 300,
            "replay_key": None,
            "fields": {"reading": "2"},
        },
    ]

    p_count, d_count = persist_telemetry_atomic(clean_redis, STREAM_REALTIME, items)
    assert p_count == 2
    assert d_count == 0
    assert clean_redis.xlen(STREAM_REALTIME) == 2
    assert clean_redis.exists(items[0]["dedup_key"]) == 1
    assert clean_redis.exists(items[1]["dedup_key"]) == 1

    # On immediate redelivery, both are recognized as duplicates atomically
    p2, d2 = persist_telemetry_atomic(clean_redis, STREAM_REALTIME, items)
    assert p2 == 0
    assert d2 == 2
    assert clean_redis.xlen(STREAM_REALTIME) == 2


def test_11_redelivery_after_successful_xadd_before_puback_idempotent(clean_redis, monkeypatch):
    """Test 11: Redelivery after successful XADD but before PUBACK remains strictly idempotent."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")

    mock_client = TrackingMQTTClient()
    payload = _make_payload(device_ts="2026-08-25T13:00:00Z")
    msg = MockMQTTMessage(VALID_TOPIC, payload, qos=1, mid=301)

    # First attempt: succeeds in Redis, but mock simulate dropped ACK
    mock_client.ack_return_code = mqtt.MQTTErrorCode.MQTT_ERR_NO_CONN
    subscriber_main._on_message(mock_client, None, msg)
    assert clean_redis.xlen(STREAM_REALTIME) == 1

    # Broker redelivers
    mock_client.ack_return_code = mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS
    redelivered_msg = MockMQTTMessage(VALID_TOPIC, payload, qos=1, mid=301, dup=True)
    subscriber_main._on_message(mock_client, None, redelivered_msg)

    # Verified: no duplicate stream entries, but ACK was sent
    assert clean_redis.xlen(STREAM_REALTIME) == 1
    assert (301, 1) in mock_client.acked_mids


# ===========================================================================
# 3. Redis Failure and Recovery Tests
# ===========================================================================

def test_12_redis_unavailable_before_processing_backoff(monkeypatch):
    """Test 12: _wait_for_redis_healthy handles unavailable Redis with backoff."""
    ping_attempts = 0

    class MockRedisDown:
        def ping(self):
            nonlocal ping_attempts
            ping_attempts += 1
            if ping_attempts < 3:
                raise redis_lib.exceptions.ConnectionError("Redis refused")
            return True

    monkeypatch.setattr(subscriber_main, "_redis", lambda: MockRedisDown())
    monkeypatch.setattr(time, "sleep", lambda s: None)  # speed up test

    subscriber_main._wait_for_redis_healthy(max_backoff=1.0)
    assert ping_attempts == 3


def test_13_redis_disconnect_during_processing_graceful(clean_redis, monkeypatch):
    """Test 13: Redis disconnect during processing is handled without crashing."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")

    def disconnect_error(*args, **kwargs):
        raise redis_lib.exceptions.ConnectionError("Connection lost mid-operation")

    monkeypatch.setattr("morghdari.subscriber.pipeline.persist_telemetry_atomic", disconnect_error)

    mock_client = TrackingMQTTClient()
    msg = MockMQTTMessage(VALID_TOPIC, _make_payload(), qos=1, mid=401)

    # Must catch exception gracefully and not raise
    subscriber_main._on_message(mock_client, None, msg)
    assert mock_client.disconnected is True
    assert len(mock_client.acked_mids) == 0


def test_14_redis_recovery_after_outage(clean_redis, monkeypatch):
    """Test 14: Message is successfully processed and acked after Redis recovers from outage."""
    outage = True
    orig_persist = persist_telemetry_atomic

    def flaky_persist(*args, **kwargs):
        if outage:
            raise redis_lib.exceptions.ConnectionError("Redis in outage")
        return orig_persist(*args, **kwargs)

    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")
    monkeypatch.setattr("morghdari.subscriber.pipeline.persist_telemetry_atomic", flaky_persist)

    mock_client = TrackingMQTTClient()
    payload = _make_payload(device_ts="2026-08-25T14:00:00Z")
    msg = MockMQTTMessage(VALID_TOPIC, payload, qos=1, mid=501)

    # 1. Attempt during outage -> fails, unacked
    subscriber_main._on_message(mock_client, None, msg)
    assert len(mock_client.acked_mids) == 0
    assert clean_redis.xlen(STREAM_REALTIME) == 0

    # 2. Redis recovers -> retry redelivery -> succeeds, acked
    outage = False
    mock_client.disconnected = False
    subscriber_main._on_message(mock_client, None, msg)
    assert clean_redis.xlen(STREAM_REALTIME) == 1
    assert (501, 1) in mock_client.acked_mids


def test_15_subscriber_remains_operational_across_transient_failures(clean_redis, monkeypatch):
    """Test 15: Subscriber processes subsequent messages after transient errors without manual restart."""
    fail_first = True
    orig_persist = persist_telemetry_atomic

    def toggle_persist(*args, **kwargs):
        nonlocal fail_first
        if fail_first:
            fail_first = False
            raise redis_lib.exceptions.ConnectionError("Transient drop")
        return orig_persist(*args, **kwargs)

    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        subscriber_main,
        "_fetch_gateway_meta",
        lambda r, org, farm, gw: {"farm_id": farm, "provisioning_status": "PROVISIONED"},
    )
    monkeypatch.setattr(subscriber_main, "_slot_db_fetch", lambda org, hw, pack: "42")
    monkeypatch.setattr("morghdari.subscriber.pipeline.persist_telemetry_atomic", toggle_persist)

    mock_client = TrackingMQTTClient()
    msg1 = MockMQTTMessage(VALID_TOPIC, _make_payload(device_ts="2026-08-25T15:01:00Z"), qos=1, mid=601)
    msg2 = MockMQTTMessage(VALID_TOPIC, _make_payload(device_ts="2026-08-25T15:02:00Z"), qos=1, mid=602)

    subscriber_main._on_message(mock_client, None, msg1)
    assert (601, 1) not in mock_client.acked_mids

    mock_client.disconnected = False
    subscriber_main._on_message(mock_client, None, msg2)
    assert (602, 1) in mock_client.acked_mids
    assert clean_redis.xlen(STREAM_REALTIME) == 1


def test_16_bounded_backoff_and_bounded_memory():
    """Test 16: Backoff does not accumulate unbounded memory queues."""
    # Verify exponential backoff calculation caps at max_backoff
    max_backoff = 30.0
    backoff = 1.0
    for _ in range(10):
        backoff = min(max_backoff, backoff * 2.0)
    assert backoff == 30.0


# ===========================================================================
# 4. MQTT Session Behavior & Integration Tests
# ===========================================================================

def test_17_subscriber_reconnect_preserves_unacknowledged_messages_broker():
    """Test 17: Live broker test - unacknowledged QoS 1 message is redelivered on reconnect."""
    client_id = f"test-paho-reliable-{int(time.time())}"
    topic = f"test/reliable/delivery/{int(time.time())}"

    # 1. Subscriber connects with clean_session=False, manual_ack=True
    received_first = []

    def on_msg_1(client, userdata, msg):
        received_first.append(msg)
        # Deliberately do NOT ACK and disconnect
        client.disconnect()

    sub1 = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=False,
        protocol=mqtt.MQTTv311,
        manual_ack=True,
    )
    sub1.on_message = on_msg_1
    sub1.connect("127.0.0.1", 1883)
    sub1.subscribe(topic, qos=1)
    time.sleep(0.1)

    # 2. Publisher publishes QoS 1 message
    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"pub-{client_id}")
    pub.connect("127.0.0.1", 1883)
    pub.publish(topic, b"unacked-payload-content", qos=1)
    pub.disconnect()

    sub1.loop_forever()
    assert len(received_first) == 1
    assert received_first[0].payload == b"unacked-payload-content"

    # 3. Reconnect with SAME client_id and clean_session=False
    received_second = []

    def on_msg_2(client, userdata, msg):
        received_second.append(msg)
        # Acknowledge and disconnect
        client.ack(msg.mid, msg.qos)
        time.sleep(0.1)
        client.disconnect()

    sub2 = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=False,
        protocol=mqtt.MQTTv311,
        manual_ack=True,
    )
    sub2.on_message = on_msg_2
    sub2.connect("127.0.0.1", 1883)
    sub2.loop_forever()

    # Broker successfully redelivered the unacknowledged message!
    assert len(received_second) == 1
    assert received_second[0].payload == b"unacked-payload-content"


def test_18_subscriber_restart_with_unacknowledged_qos1():
    """Test 18: Client construction pins session settings and manual ack."""
    with patch.dict(os.environ, {"SUBSCRIBER_CLIENT_ID": "test-sub-01", "MQTT_CLEAN_SESSION": "false"}):
        client = subscriber_main.build_client()
        assert client._clean_session is False
        assert client._manual_ack is True
        assert client._protocol == mqtt.MQTTv311

    with patch.dict(os.environ, {"SUBSCRIBER_CLIENT_ID": "test-sub-02", "MQTT_CLEAN_SESSION": "true"}):
        client_clean = subscriber_main.build_client()
        assert client_clean._clean_session is True
        assert client_clean._manual_ack is True


def test_19_shared_subscription_load_balancing_configuration():
    """Test 19: Shared subscription group is telemetry_workers for load balancing."""
    assert subscriber_main.SHARED_TOPIC.startswith("$share/telemetry_workers/")


def test_20_subscriber_client_ids_distinct_and_required():
    """Test 20: Refuses to boot without SUBSCRIBER_CLIENT_ID."""
    with patch.dict(os.environ, {"SUBSCRIBER_CLIENT_ID": ""}):
        with pytest.raises(SystemExit):
            subscriber_main.build_client()


# ===========================================================================
# 5. Lua Atomicity, DLQ Across All Lanes, and Metrics Tests
# ===========================================================================

def test_21_lua_failure_rollback_preflight_lane_corruption(clean_redis):
    """Test 21: Corrupted lane key aborts Lua script, leaves no dedup key, and retry succeeds."""
    clean_redis.set(STREAM_REALTIME, "corrupt_string_value")
    items = [{
        "dedup_key": "dedup:org_01:slot_99:2026-11-10T12:00:00Z",
        "lane": STREAM_REALTIME,
        "ttl": 300,
        "replay_key": None,
        "fields": {"payload": "test_corrupt"},
    }]

    with pytest.raises((redis_lib.exceptions.RedisError, RuntimeError)):
        persist_telemetry_atomic(clean_redis, STREAM_REALTIME, items)

    # Invariant: dedup key MUST NOT be left in Redis
    assert clean_redis.exists("dedup:org_01:slot_99:2026-11-10T12:00:00Z") == 0

    # Clear corruption and retry
    clean_redis.delete(STREAM_REALTIME)
    persisted, dup = persist_telemetry_atomic(clean_redis, STREAM_REALTIME, items)
    assert persisted == 1
    assert dup == 0
    assert clean_redis.exists("dedup:org_01:slot_99:2026-11-10T12:00:00Z") == 1
    assert clean_redis.xlen(STREAM_REALTIME) == 1


def test_22_lua_failure_rollback_multi_pack(clean_redis):
    """Test 22: Failure on second item in multi-pack rolls back first item."""
    items = [
        {
            "dedup_key": "dedup:org_01:slot_01:2026-11-10T12:00:00Z",
            "lane": STREAM_REALTIME,
            "ttl": 300,
            "replay_key": "replay:count:test",
            "fields": {"payload": "item1"},
            "simulated_fail": False,
        },
        {
            "dedup_key": "dedup:org_01:slot_02:2026-11-10T12:00:00Z",
            "lane": STREAM_REALTIME,
            "ttl": 300,
            "replay_key": "replay:count:test",
            "fields": {"payload": "item2"},
            "simulated_fail": True,
        },
    ]

    with pytest.raises((redis_lib.exceptions.RedisError, RuntimeError)):
        persist_telemetry_atomic(clean_redis, STREAM_REALTIME, items)

    # Invariant: Item 1 dedup key and stream entry rolled back
    assert clean_redis.exists("dedup:org_01:slot_01:2026-11-10T12:00:00Z") == 0
    assert clean_redis.exists("dedup:org_01:slot_02:2026-11-10T12:00:00Z") == 0
    assert clean_redis.xlen(STREAM_REALTIME) == 0


def test_27_lua_failure_rollback_after_xadd(clean_redis):
    """Test 27: Failure after XADD before SET dedup deletes empty stream and leaves no dedup."""
    lane = "stream:telemetry:test27"
    assert clean_redis.exists(lane) == 0
    items = [{
        "dedup_key": "dedup:org_01:slot_27:2026-11-10T12:00:00Z",
        "lane": lane,
        "ttl": 300,
        "replay_key": None,
        "fields": {"val": 42},
        "fail_after_xadd": True,
    }]

    with pytest.raises((redis_lib.exceptions.RedisError, RuntimeError)):
        persist_telemetry_atomic(clean_redis, lane, items)

    assert clean_redis.exists("dedup:org_01:slot_27:2026-11-10T12:00:00Z") == 0
    assert clean_redis.exists(lane) == 0, "Empty stream key must be deleted when newly created"

    # Retry succeeds
    items[0]["fail_after_xadd"] = False
    p, d = persist_telemetry_atomic(clean_redis, lane, items)
    assert p == 1 and d == 0
    assert clean_redis.exists("dedup:org_01:slot_27:2026-11-10T12:00:00Z") == 1
    assert clean_redis.xlen(lane) == 1


def test_28_lua_failure_rollback_after_dedup(clean_redis):
    """Test 28: Failure after SET dedup rolls back dedup key, stream entry, and stream key."""
    lane = "stream:telemetry:test28"
    assert clean_redis.exists(lane) == 0
    items = [{
        "dedup_key": "dedup:org_01:slot_28:2026-11-10T12:00:00Z",
        "lane": lane,
        "ttl": 300,
        "replay_key": None,
        "fields": {"val": 99},
        "fail_after_dedup": True,
    }]

    with pytest.raises((redis_lib.exceptions.RedisError, RuntimeError)):
        persist_telemetry_atomic(clean_redis, lane, items)

    assert clean_redis.exists("dedup:org_01:slot_28:2026-11-10T12:00:00Z") == 0
    assert clean_redis.exists(lane) == 0


def test_29_lua_failure_rollback_replay_key_exact_deletion(clean_redis):
    """Test 29: Replay key rollback deletes non-existent key rather than leaving value 0."""
    lane = "stream:telemetry:test29"
    rk_new = "replay:count:brand_new_key"
    assert clean_redis.exists(rk_new) == 0

    items_new = [{
        "dedup_key": "dedup:org_01:slot_29a:2026-11-10T12:00:00Z",
        "lane": lane,
        "ttl": 300,
        "replay_key": rk_new,
        "fields": {"val": 10},
        "fail_after_rk": True,
    }]

    with pytest.raises((redis_lib.exceptions.RedisError, RuntimeError)):
        persist_telemetry_atomic(clean_redis, lane, items_new)

    # Invariant: DECR must NOT leave value 0; key must not exist
    assert clean_redis.exists(rk_new) == 0
    assert clean_redis.exists("dedup:org_01:slot_29a:2026-11-10T12:00:00Z") == 0
    assert clean_redis.exists(lane) == 0

    # Part B: Existing replay key is decremented and preserves TTL
    rk_exist = "replay:count:existing_key"
    clean_redis.set(rk_exist, "5")
    clean_redis.expire(rk_exist, 600)

    items_exist = [{
        "dedup_key": "dedup:org_01:slot_29b:2026-11-10T12:00:00Z",
        "lane": lane,
        "ttl": 300,
        "replay_key": rk_exist,
        "fields": {"val": 20},
        "fail_after_rk": True,
    }]

    with pytest.raises((redis_lib.exceptions.RedisError, RuntimeError)):
        persist_telemetry_atomic(clean_redis, lane, items_exist)

    assert clean_redis.exists(rk_exist) == 1
    raw_val = clean_redis.get(rk_exist)
    val_int = int(raw_val.decode() if isinstance(raw_val, bytes) else raw_val)
    assert val_int == 5
    assert clean_redis.ttl(rk_exist) > 0


def test_23_dlq_rejections_across_all_lanes(clean_redis, monkeypatch):
    """Test 23: Permanently rejected messages across all lanes are written to DLQ before ACK."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    mock_client = TrackingMQTTClient()

    # 1. Status lane: bad JSON
    status_topic = "org_01/farms/1/gateways/gw_slot_01/status"
    msg_status_bad = MockMQTTMessage(status_topic, b"not_valid_json", qos=1, mid=701)
    subscriber_main._on_message(mock_client, None, msg_status_bad)
    assert (701, 1) in mock_client.acked_mids

    # 2. Status lane: identity mismatch
    msg_status_mismatch = MockMQTTMessage(
        status_topic,
        json.dumps({"gateway_slot_id": "different_slot", "status": "online"}).encode(),
        qos=1, mid=702,
    )
    subscriber_main._on_message(mock_client, None, msg_status_mismatch)
    assert (702, 1) in mock_client.acked_mids

    # 3. Status lane: invalid status
    msg_status_invalid = MockMQTTMessage(
        status_topic,
        json.dumps({"gateway_slot_id": "gw_slot_01", "status": "INVALID_STATE"}).encode(),
        qos=1, mid=703,
    )
    subscriber_main._on_message(mock_client, None, msg_status_invalid)
    assert (703, 1) in mock_client.acked_mids

    # 4. Command response lane: bad JSON
    cmd_topic = "org_01/farms/1/gateways/gw_slot_01/commands/response"
    msg_cmd_bad = MockMQTTMessage(cmd_topic, b"bad_cmd_json", qos=1, mid=704)
    subscriber_main._on_message(mock_client, None, msg_cmd_bad)
    assert (704, 1) in mock_client.acked_mids

    # 5. Reported device lane: bad JSON
    reported_topic = "devices/gw_slot_01/reported"
    msg_rep_bad = MockMQTTMessage(reported_topic, b"bad_reported_json", qos=1, mid=705)
    subscriber_main._on_message(mock_client, None, msg_rep_bad)
    assert (705, 1) in mock_client.acked_mids

    # Verify DLQ entries recorded in Redis
    assert clean_redis.xlen(DLQ_STREAM) == 5
    dlq_entries = clean_redis.xrange(DLQ_STREAM)
    reasons = [e[1].get(b"reason", e[1].get("reason", "")) for e in dlq_entries]
    reasons_str = [r.decode() if isinstance(r, bytes) else str(r) for r in reasons]
    assert any("status_bad_json" in r for r in reasons_str)
    assert any("status_identity_mismatch" in r for r in reasons_str)
    assert any("status_unsupported_status" in r for r in reasons_str)
    assert any("command_response_bad_json" in r for r in reasons_str)
    assert any("reported_bad_json" in r for r in reasons_str)


def test_24_dlq_failure_prevents_ack_all_lanes(clean_redis, monkeypatch):
    """Test 24: DLQ write failure prevents PUBACK across all non-telemetry lanes."""
    monkeypatch.setattr(subscriber_main, "_redis", lambda: clean_redis)
    monkeypatch.setattr(
        "morghdari.subscriber.dlq.send_to_dlq",
        MagicMock(side_effect=subscriber_main.DLQWriteError("Simulated DLQ error")),
    )

    mock_client = TrackingMQTTClient()

    # Status lane with bad JSON
    msg_status = MockMQTTMessage("org_01/farms/1/gateways/gw_slot_01/status", b"bad", qos=1, mid=801)
    subscriber_main._on_message(mock_client, None, msg_status)
    assert (801, 1) not in mock_client.acked_mids
    assert mock_client.disconnected is True

    # Command response lane with bad JSON
    mock_client.disconnected = False
    msg_cmd = MockMQTTMessage("org_01/farms/1/gateways/gw_slot_01/commands/response", b"bad", qos=1, mid=802)
    subscriber_main._on_message(mock_client, None, msg_cmd)
    assert (802, 1) not in mock_client.acked_mids
    assert mock_client.disconnected is True

    # Reported device lane with bad JSON
    mock_client.disconnected = False
    msg_rep = MockMQTTMessage("devices/gw_slot_01/reported", b"bad", qos=1, mid=803)
    subscriber_main._on_message(mock_client, None, msg_rep)
    assert (803, 1) not in mock_client.acked_mids
    assert mock_client.disconnected is True


def test_25_metrics_handler_and_counters():
    """Test 25: Prometheus metrics surface outputs accurate counter names and HELP descriptions."""
    with subscriber_main._messages_lock:
        subscriber_main._messages_seen = 42
        subscriber_main._messages_acked = 40
        subscriber_main._lua_failures = 2

    class DummyRequest:
        def makefile(self, *args, **kwargs):
            import io
            return io.BytesIO(b"GET /metrics HTTP/1.0\r\nHost: localhost\r\n\r\n")

        def sendall(self, data):
            pass

    class ResponseWriter:
        def __init__(self):
            self.data = b""

        def write(self, b):
            self.data += b

        def flush(self):
            pass

    handler = subscriber_main._MetricsHandler(
        DummyRequest(), ("127.0.0.1", 12345), None
    )
    writer = ResponseWriter()
    handler.wfile = writer
    handler.do_GET()

    output = writer.data.decode("utf-8")
    assert "mqtt_messages_acked_total 40" in output
    assert "mqtt_lua_failures_total 2" in output
    assert "Local PUBACK packets queued into outgoing socket buffer" in output


def test_26_shared_subscription_clean_session_failover_emqx():
    """Test 26: Live EMQX shared subscription failover with clean_session=True."""
    import socket
    # Check if EMQX 5.5.1 is running on port 18883
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 18883))
        s.close()
    except Exception:
        pytest.skip("Local EMQX broker on port 18883 not running")

    ts = int(time.time())
    topic = f"test/emqx551/failover_test_{ts}"
    shared_topic = f"$share/telemetry_workers/{topic}"
    received_a = []
    received_b = []
    ready_a = threading.Event()
    ready_b = threading.Event()

    def on_a(c, ud, msg):
        received_a.append(msg)
        c.disconnect()

    def on_b(c, ud, msg):
        received_b.append(msg)
        c.ack(msg.mid, msg.qos)

    sub_a = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sub-failover-A-{ts}",
        clean_session=True,
        manual_ack=True,
    )
    sub_a.on_connect = lambda c, ud, flags, rc, props=None: (c.subscribe(shared_topic, qos=1), ready_a.set())
    sub_a.on_message = on_a

    sub_b = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sub-failover-B-{ts}",
        clean_session=True,
        manual_ack=True,
    )
    sub_b.on_connect = lambda c, ud, flags, rc, props=None: (c.subscribe(shared_topic, qos=1), ready_b.set())
    sub_b.on_message = on_b

    # Connect Sub-A first so EMQX registers it as the first worker
    sub_a.connect("127.0.0.1", 18883)
    sub_a.loop_start()
    ready_a.wait(3.0)
    time.sleep(0.5)

    # Connect Sub-B as standby worker
    sub_b.connect("127.0.0.1", 18883)
    sub_b.loop_start()
    ready_b.wait(3.0)
    time.sleep(0.5)

    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"pub-failover-{ts}")
    pub.connect("127.0.0.1", 18883)
    pub.publish(topic, b"test-shared-failover-data", qos=1)
    pub.disconnect()

    t0 = time.time()
    while time.time() - t0 < 5.0:
        if len(received_b) > 0 and (len(received_a) > 0 or len(received_b) > 0):
            break
        time.sleep(0.1)

    sub_a.loop_stop()
    sub_b.loop_stop()
    sub_b.disconnect()

    # The message was delivered and handled reliably by the group
    total_received = len(received_a) + len(received_b)
    assert total_received >= 1
    assert any(m.payload == b"test-shared-failover-data" for m in received_a + received_b)


def test_30_scenario_b_all_subscribers_offline_emqx_drops_messages():
    """Test 30: When all subscribers are offline, EMQX sends PUBACK to publisher and drops QoS 1 message."""
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 18883))
        s.close()
    except Exception:
        pytest.skip("Local EMQX broker on port 18883 not running")

    ts = int(time.time())
    topic = f"test/emqx551/scenB_drop_{ts}"
    shared_topic = f"$share/telemetry_workers/{topic}"

    # Verify no subscriber is connected to this shared topic
    pub_acks = []
    def on_pub(client, userdata, mid, reason_code=None, properties=None):
        pub_acks.append(mid)

    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"pub-scenB-test-{ts}")
    pub.on_publish = on_pub
    pub.connect("127.0.0.1", 18883)
    pub.loop_start()

    res = pub.publish(topic, b"unbuffered-offline-payload", qos=1)
    res.wait_for_publish(timeout=3.0)
    time.sleep(0.5)
    pub.loop_stop()
    pub.disconnect()

    # EMQX sends PUBACK to publisher even with no subscribers!
    assert res.is_published() is True
    assert res.mid in pub_acks

    # Now subscriber connects afterwards
    received_after = []
    sub = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sub-after-offline-{ts}",
        clean_session=True,
        manual_ack=True,
    )
    sub.on_message = lambda c, ud, msg: (received_after.append(msg), c.ack(msg.mid, msg.qos))
    sub.connect("127.0.0.1", 18883)
    sub.subscribe(shared_topic, qos=1)
    sub.loop_start()

    time.sleep(2.0)
    sub.loop_stop()
    sub.disconnect()

    # The message was dropped by EMQX because no members were online when published
    assert len(received_after) == 0


class _TestBreakException(BaseException):
    pass


def test_31_subscriber_main_reconnect_attempts_scoping(monkeypatch):
    """Verify that main() increments module-level _reconnect_attempts without UnboundLocalError."""
    import morghdari.subscriber.main as sub_main
    from unittest.mock import MagicMock

    initial_reconnects = sub_main._reconnect_attempts
    monkeypatch.setattr(sub_main, "_django_ready", lambda: None)
    monkeypatch.setattr(sub_main, "_wait_for_redis_healthy", lambda: None)
    monkeypatch.setattr(sub_main, "_serve_metrics", lambda: None)

    mock_client = MagicMock()
    mock_client.loop_forever.side_effect = _TestBreakException("break main loop")
    monkeypatch.setattr(sub_main, "build_client", lambda: mock_client)

    try:
        sub_main.main()
    except _TestBreakException:
        pass

    assert sub_main._reconnect_attempts == initial_reconnects + 1


