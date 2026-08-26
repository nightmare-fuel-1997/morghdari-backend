"""M3-02 acceptance â€” topic identity + packâ†’slot resolution, tests FIRST.

WHY: the transport is the truth (TUTORIAL Â§3.1): org/farm/gateway come FROM
THE TOPIC because the broker authenticated that path; the single mirrored
payload field must match or the message is an identity fault; and packâ†’slot
resolution goes cache-first (24 h) with DB fallback, dead-lettering unknowns.
Forgery/malformation tests are written BEFORE the implementation â€” security
before throughput.

ÙØ§Ø±Ø³ÛŒ: Ù¾Ø°ÛŒØ±Ø´ M3-02 â€” Ù‡ÙˆÛŒØª Ø§Ø² ØªØ§Ù¾ÛŒÚ©ØŒ ØªØ·Ø¨ÛŒÙ‚Ù Ø´Ù†Ø§Ø³Ù‡ Ø¨Ø§ Ù…Ø­Ù…ÙˆÙ„Ù‡ Ùˆ Ø­Ù„Ù Ù¾Ú© Ø¨Ù‡ Ø§Ø³Ù„Ø§Øª Ø¨Ø§
Ú©ÛŒØ´ Ø§ÙˆÙ„ Ùˆ Ø¯ÛŒØªØ§Ø¨ÛŒØ³ Ø¯Ø± Ù¾Ø´ØªÛŒØ¨Ø§Ù†ÛŒØ› ØªØ³Øªâ€ŒÙ‡Ø§ Ù¾ÛŒØ´ Ø§Ø² Ù¾ÛŒØ§Ø¯Ù‡â€ŒØ³Ø§Ø²ÛŒ Ù†ÙˆØ´ØªÙ‡ Ø´Ø¯Ù‡â€ŒØ§Ù†Ø¯.
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

# Dedicated SCRATCH logical DB (never the demo/dev db0): wiped per test.
from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL  # noqa: E402

VALID_TOPIC = (
    "9f8c2d34-6f0e-4a55-9a10-2b7c1f5e4d01/farms/12/gateways/FARM-01-GW-A/telemetry"
)


@pytest.fixture()
def redis_client():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    # Hermetic: scrub BEFORE and AFTER â€” the dev/demo Redis is long-lived and
    # pytest processes come and go, so leftovers poison the next run (dedup
    # keys live 25h; streams keep every crashed run's junk). Globs: cache keys
    # look like "slot:map:m3test-HW:P-A" (wildcard right after the tag); dedup
    # keys carry the test org uuid. The two lanes wipe wholesale; DLQ is
    # shared with other assertions â€” never wiped here.
    for pattern in ("slot:map:m3test*", "dedup:9f8c2d34-6f0e*"):
        for key in client.scan_iter(pattern):
            client.delete(key)
    client.delete("telemetry:realtime")
    client.delete("telemetry:historical")
    yield client
    for pattern in ("slot:map:m3test*", "dedup:9f8c2d34-6f0e*"):
        for key in client.scan_iter(pattern):
            client.delete(key)
    client.delete("telemetry:realtime")
    client.delete("telemetry:historical")


# â”€â”€ parser â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
    # The field is load-bearing per Firmware Spec Â§3.8; absence is a fault too.
    assert identity_fault("FARM-01-GW-A", {}) == "TOPIC_PAYLOAD_IDENTITY_MISMATCH"


def test_identity_ok_when_slots_agree():
    payload = {"gateway_slot_id": "FARM-01-GW-A"}
    assert identity_fault("FARM-01-GW-A", payload) is None


# â”€â”€ resolver â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_cache_key_namespaces_by_hardware_and_pack():
    assert cache_key("AA:BB", "P-A") == "slot:map:AA:BB:P-A"


def test_resolver_cache_miss_uses_db_and_populates_cache(redis_client):
    calls = []

    def fake_db(org, hw, pack):
        calls.append((org, hw, pack))
        return "42"

    first = resolve_pack_slot(
        redis_client, "org-1", "m3test-HW", "P-A", db_fetch=fake_db
    )
    second = resolve_pack_slot(
        redis_client, "org-1", "m3test-HW", "P-A", db_fetch=fake_db
    )
    assert (first, second) == ("42", "42")  # slot ids are CharField pks
    assert calls == [("org-1", "m3test-HW", "P-A")]  # DB touched ONCE
    assert redis_client.ttl(cache_key("m3test-HW", "P-A")) > 80_000  # ~24 h


def test_resolver_unknown_pack_returns_sentinel_without_caching(redis_client):
    def no_rows(org, hw, pack):
        return None

    result = resolve_pack_slot(
        redis_client, "org-1", "m3test-HW", "P-MISSING", db_fetch=no_rows
    )
    assert result == UNKNOWN_SLOT
    # A miss must NOT poison the cache â€” provisioning may create the row later.
    assert redis_client.exists(cache_key("m3test-HW", "P-MISSING")) == 0


# â”€â”€ DLQ writer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_dlq_entry_carries_topic_payload_reason(redis_client):
    entry_id = send_to_dlq(redis_client, VALID_TOPIC, b"{not json", "bad_json")
    entries = redis_client.xrange(DLQ_STREAM, min=entry_id, max=entry_id)
    _, fields = entries[0]
    assert fields["topic"] == VALID_TOPIC
    assert fields["payload"] == "{not json"
    assert fields["reason"] == "bad_json"
    redis_client.xdel(DLQ_STREAM, entry_id)  # keep dev stream clean


# â”€â”€ pipeline wiring (parse â†’ identity â†’ resolve, pre-dedup skeleton) â”€â”€â”€â”€â”€â”€

def _valid_payload(**overrides):
    """Minimal schema-2.0 body the gates accept (single reading, mapped pack)."""
    payload = {
        "gateway_hardware_id": "m3test-HW",
        "gateway_slot_id": "FARM-01-GW-A",
        "schema_version": "2.0",
        "device_ts": "2026-08-25T10:30:00Z",
        "is_buffered": False,
        "readings": [{"pack_id": "P-A", "metrics": {"temp": {"avg": 28.4}}}],
    }
    payload.update(overrides)
    return payload


def _dlq_reason(redis_client, entry_id):
    _, fields = redis_client.xrange(DLQ_STREAM, min=entry_id, max=entry_id)[0]
    return fields["reason"]


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
    assert _dlq_reason(redis_client, entry_id) == "TOPIC_PAYLOAD_IDENTITY_MISMATCH"
    redis_client.xdel(DLQ_STREAM, entry_id)


# â”€â”€ M3-03: schema gate, dedup guard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_handle_message_bad_json_goes_to_dlq(redis_client):
    entry_id = handle_message(redis_client, VALID_TOPIC, b"{not json")
    assert _dlq_reason(redis_client, entry_id) == "bad_json"
    redis_client.xdel(DLQ_STREAM, entry_id)


def test_handle_message_non_object_json_is_bad_json(redis_client):
    entry_id = handle_message(redis_client, VALID_TOPIC, b"[1,2,3]")
    assert _dlq_reason(redis_client, entry_id) == "bad_json"
    redis_client.xdel(DLQ_STREAM, entry_id)


def test_unsupported_schema_version_dead_letters(redis_client):
    entry_id = handle_message(
        redis_client,
        VALID_TOPIC,
        _valid_payload_bytes(schema_version="1.0"),
    )
    assert _dlq_reason(redis_client, entry_id) == "UNSUPPORTED_SCHEMA_VERSION"
    redis_client.xdel(DLQ_STREAM, entry_id)


def test_missing_device_ts_dead_letters(redis_client):
    body = _valid_payload()
    del body["device_ts"]
    entry_id = handle_message(redis_client, VALID_TOPIC, _bytes(body))
    assert _dlq_reason(redis_client, entry_id) == "missing_device_ts"
    redis_client.xdel(DLQ_STREAM, entry_id)


def test_duplicate_payload_dropped_silently_before_resolution(redis_client):
    calls = []
    body = _valid_payload()

    first = handle_message(
        redis_client, VALID_TOPIC, _bytes(body), db_fetch=lambda *a: "42"
    )

    def counting(*args):
        calls.append(args)
        return 42

    second = handle_message(redis_client, VALID_TOPIC, _bytes(body), db_fetch=counting)
    assert first is None  # happy path accepted
    assert second is None  # duplicate dropped â€” NOT dead-lettered
    assert calls == []  # resolution served from 24h cache: zero DB work on dup

    # Per-resolved-slot key (Blueprint Â§4:131 verbatim shape).
    key = "dedup:9f8c2d34-6f0e-4a55-9a10-2b7c1f5e4d01:42:" + body["device_ts"]
    ttl = redis_client.ttl(key)
    assert ttl >= 86_000  # 90000s invariant â‰¥ EMQX session expiry


def test_different_device_ts_is_not_a_duplicate(redis_client):
    calls = []
    fetcher = lambda *a: "42"  # noqa: E731

    handle_message(
        redis_client,
        VALID_TOPIC,
        _bytes(_valid_payload(device_ts="2026-08-25T10:30:00Z")),
        db_fetch=fetcher,
    )
    second = handle_message(
        redis_client,
        VALID_TOPIC,
        _bytes(_valid_payload(device_ts="2026-08-25T10:31:00Z")),
        db_fetch=lambda *a: calls.append(1) or 43,
    )
    assert second is None
    # A genuinely new window must be PROCESSED again â€” proof is the stream:
    # two windows â†’ two enriched entries (resolution itself may come from the
    # 24h cache, which is correct and cheap; DB is only the cold path).
    entries = _drain_stream(redis_client, REALTIME)
    assert sorted(f["device_ts"] for _, f in entries) == [
        "2026-08-25T10:30:00Z",
        "2026-08-25T10:31:00Z",
    ]


# â”€â”€ M3-04: enrichment â†’ lane streams Â· M3-07: trimming â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

REALTIME = "telemetry:realtime"
HISTORICAL = "telemetry:historical"


def _drain_stream(client, stream):
    entries = client.xrange(stream)
    client.delete(stream)
    return [(i, f) for i, f in entries]


def test_enriched_realtime_entry_carries_contract_fields(redis_client):
    body = _valid_payload()
    handle_message(redis_client, VALID_TOPIC, _bytes(body), db_fetch=lambda *a: "42")

    entries = _drain_stream(redis_client, REALTIME)
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["org_id"] == "9f8c2d34-6f0e-4a55-9a10-2b7c1f5e4d01"
    assert fields["farm_id"] == "12"
    assert fields["gateway_slot_id"] == "FARM-01-GW-A"
    assert fields["device_ts"] == body["device_ts"]
    assert fields["is_buffered"] == "False"  # redis stores strings
    assert fields["slot_id"] == "42"         # resolved, NOT from the wire
    assert '"pack_id"' in fields["payload"]  # raw JSON travels intact
    assert float(fields["recv_ts"]) > 0
    assert len(fields["trace_id"]) == 36     # uuid4 tracking sticker


def test_multi_pack_payload_emits_one_entry_per_reading(redis_client):
    body = _valid_payload(
        readings=[
            {"pack_id": "P-A", "metrics": {"temp": {"avg": 28.4}}},
            {"pack_id": "P-B", "metrics": {"nh3": {"avg": 14.0}}},
        ]
    )
    def fetch(org, hw, pack):
        return {"P-A": 42, "P-B": 43}[pack]

    handle_message(redis_client, VALID_TOPIC, _bytes(body), db_fetch=fetch)
    entries = _drain_stream(redis_client, REALTIME)
    assert sorted(f["slot_id"] for _, f in entries) == ["42", "43"]


def test_buffered_payload_routes_to_historical_lane(redis_client):
    handle_message(
        redis_client,
        VALID_TOPIC,
        _bytes(_valid_payload(is_buffered=True)),
        db_fetch=lambda *a: "42",
    )
    assert redis_client.xlen(REALTIME) == 0
    entries = _drain_stream(redis_client, HISTORICAL)
    assert len(entries) == 1
    assert entries[0][1]["is_buffered"] == "True"


def test_trim_issues_approximate_xtrim_on_both_lanes(monkeypatch, redis_client):
    # NOTE: we spy the COMMAND, not the resulting length â€” Redis's approximate
    # XTRIM may legally undershoot when entries share radix nodes (observed:
    # 25-entry stream in one node trims to... 25). The 100k bound is asserted
    # by the sustained-load acceptance in M3-10 on a realistic volume.
    from morghdari.subscriber import pipeline

    calls = []
    real_xtrim = redis_client.xtrim

    def spy(name, maxlen=None, approximate=True, **kw):
        calls.append((name, maxlen, approximate))
        return real_xtrim(name, maxlen=maxlen, approximate=approximate)

    monkeypatch.setattr(redis_client, "xtrim", spy)
    redis_client.xadd(REALTIME, {"n": "1"})
    redis_client.xadd(HISTORICAL, {"n": "1"})

    pipeline.trim_streams(redis_client)

    assert ("telemetry:realtime", pipeline.STREAM_MAXLEN, True) in calls
    assert ("telemetry:historical", pipeline.STREAM_MAXLEN, True) in calls
    redis_client.delete(REALTIME, HISTORICAL)


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _valid_payload_bytes(**overrides):
    return _bytes(_valid_payload(**overrides))


def _bytes(body) -> bytes:
    import json

    return json.dumps(body).encode()



