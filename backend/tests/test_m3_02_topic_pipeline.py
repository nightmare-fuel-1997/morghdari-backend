"""M3-02 acceptance — topic identity + pack→slot resolution, tests FIRST.

WHY: the transport is the truth (TUTORIAL §3.1): org/farm/gateway come FROM
THE TOPIC because the broker authenticated that path; the single mirrored
payload field must match or the message is an identity fault; and pack→slot
resolution goes cache-first (24 h) with DB fallback, dead-lettering unknowns.
Forgery/malformation tests are written BEFORE the implementation — security
before throughput.

فارسی: پذیرش M3-02 — هویت از تاپیک، تطبیقِ شناسه با محموله و حلِ پک به اسلات با
کیش اول و دیتابیس در پشتیبانی؛ تست‌ها پیش از پیاده‌سازی نوشته شده‌اند.
"""

import os

import pytest
import redis as redis_lib

from morghdari.subscriber.dlq import DLQ_STREAM, send_to_dlq
from morghdari.subscriber.pipeline import handle_message
from morghdari.subscriber.resolver import (
    UNKNOWN_SLOT,
    CACHE_TTL_SECONDS,
    cache_key,
    resolve_pack_slot,
)
from morghdari.subscriber.topics import (
    TopicParseError,
    identity_fault,
    parse_topic,
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")

VALID_TOPIC = (
    "9f8c2d34-6f0e-4a55-9a10-2b7c1f5e4d01/farms/12/gateways/FARM-01-GW-A/telemetry"
)


@pytest.fixture()
def redis_client():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    # Hermetic: scrub BEFORE and AFTER — the dev/demo Redis is long-lived.
    # NOTE the glob: cache keys look like "slot:map:m3test-HW:P-A", so the
    # wildcard must start right after the "m3test" tag, not after a colon.
    for key in client.scan_iter("slot:map:m3test*"):
        client.delete(key)
    yield client
    for key in client.scan_iter("slot:map:m3test*"):
        client.delete(key)


# ── parser ────────────────────────────────────────────────────────────────

def test_parse_valid_doc_example_topic():
    assert parse_topic(VALID_TOPIC) == (
        "9f8c2d34-6f0e-4a55-9a10-2b7c1f5e4d01",
        "12",
        "FARM-01-GW-A",
    )


@pytest.mark.parametrize(
    "bad_topic",
    [
        "",                                            # empty
        "org/farms/12/gateways/GW/telemetry/extra",    # trailing segment
        "org/farms/12/gateways/GW",                    # truncated suffix
        "org/farms/12/gateways/GW/status",             # wrong suffix (T1 vs other)
        "/farms/12/gateways/GW/telemetry",             # empty org
        "org/farms//gateways/GW/telemetry",            # empty farm
        "org/farm/12/gateways/GW/telemetry",           # singular farm (forged shape)
    ],
)
def test_parse_malformed_topics_raise(bad_topic):
    with pytest.raises(TopicParseError):
        parse_topic(bad_topic)


def test_identity_fault_on_slot_mismatch():
    payload = {"gateway_slot_id": "OTHER-GW"}
    assert (
        identity_fault("FARM-01-GW-A", payload)
        == "TOPIC_PAYLOAD_IDENTITY_MISMATCH"
    )


def test_identity_fault_when_payload_field_absent():
    # The field is load-bearing per Firmware Spec §3.8; absence is a fault too.
    assert identity_fault("FARM-01-GW-A", {}) == "TOPIC_PAYLOAD_IDENTITY_MISMATCH"


def test_identity_ok_when_slots_agree():
    payload = {"gateway_slot_id": "FARM-01-GW-A"}
    assert identity_fault("FARM-01-GW-A", payload) is None


# ── resolver ──────────────────────────────────────────────────────────────

def test_cache_key_namespaces_by_hardware_and_pack():
    assert cache_key("AA:BB", "P-A") == "slot:map:AA:BB:P-A"


def test_resolver_cache_miss_uses_db_and_populates_cache(redis_client):
    calls = []

    def fake_db(org, hw, pack):
        calls.append((org, hw, pack))
        return 42

    first = resolve_pack_slot(
        redis_client, "org-1", "m3test-HW", "P-A", db_fetch=fake_db
    )
    second = resolve_pack_slot(
        redis_client, "org-1", "m3test-HW", "P-A", db_fetch=fake_db
    )
    assert (first, second) == (42, 42)
    assert calls == [("org-1", "m3test-HW", "P-A")]  # DB touched ONCE
    assert redis_client.ttl(cache_key("m3test-HW", "P-A")) > 80_000  # ~24 h


def test_resolver_unknown_pack_returns_sentinel_without_caching(redis_client):
    def no_rows(org, hw, pack):
        return None

    result = resolve_pack_slot(
        redis_client, "org-1", "m3test-HW", "P-MISSING", db_fetch=no_rows
    )
    assert result == UNKNOWN_SLOT
    # A miss must NOT poison the cache — provisioning may create the row later.
    assert redis_client.exists(cache_key("m3test-HW", "P-MISSING")) == 0


# ── DLQ writer ────────────────────────────────────────────────────────────

def test_dlq_entry_carries_topic_payload_reason(redis_client):
    entry_id = send_to_dlq(redis_client, VALID_TOPIC, b"{not json", "bad_json")
    entries = redis_client.xrange(DLQ_STREAM, min=entry_id, max=entry_id)
    _, fields = entries[0]
    assert fields["topic"] == VALID_TOPIC
    assert fields["payload"] == "{not json"
    assert fields["reason"] == "bad_json"
    redis_client.xdel(DLQ_STREAM, entry_id)  # keep dev stream clean


# ── pipeline wiring (parse → identity → resolve, pre-dedup skeleton) ──────

def test_handle_message_routes_malformed_topic_to_dlq(redis_client):
    entry_id = handle_message(
        redis_client, "forged/topic/shape", b'{"gateway_slot_id":"X"}'
    )
    _, fields = redis_client.xrange(DLQ_STREAM, min=entry_id, max=entry_id)[0]
    assert fields["reason"] == "bad_topic"
    redis_client.xdel(DLQ_STREAM, entry_id)


def test_handle_message_identity_mismatch_goes_to_dlq(redis_client):
    entry_id = handle_message(
        redis_client,
        VALID_TOPIC.replace("FARM-01-GW-A", "FARM-01-GW-B"),
        b'{"gateway_slot_id":"FARM-01-GW-A"}',
    )
    _, fields = redis_client.xrange(DLQ_STREAM, min=entry_id, max=entry_id)[0]
    assert fields["reason"] == "TOPIC_PAYLOAD_IDENTITY_MISMATCH"
    redis_client.xdel(DLQ_STREAM, entry_id)
