"""Per-message pipeline — the ordered gates every reading passes (M3-02..04).

Order is CONTRACTUAL (TUTORIAL §3.1–3.4, pitfalls table):
  ① parse topic → ② identity cross-check → ③ schema gate → ④ device_ts
  → ⑤ CHEAP dedup → ⑥ slot resolution → ⑦ enrich + XADD per reading
  (+ trim on the write path, M3-07).
A rejection at ANY gate dead-letters with a reason; a duplicate drops
silently as a success.
فارسی: ترتیبِ دروازه‌ها قراردادی است؛ هر رد در هر دروازه با دلیل به صندوق مرده
می‌رود و پیامِ تکراری بی‌سروصدا حذف می‌شود.
"""

import json
import time
import uuid

from .dlq import send_to_dlq
from .resolver import UNKNOWN_SLOT, resolve_pack_slot
from .topics import TopicParseError, identity_fault, parse_topic

SUPPORTED_SCHEMA_VERSION = "2.0"   # Firmware Spec §3.8 — gates the parser
DEDUP_TTL_SECONDS = 90_000         # ≥ EMQX session expiry (AU-fix invariant)
STREAM_REALTIME = "telemetry:realtime"
STREAM_HISTORICAL = "telemetry:historical"
STREAM_MAXLEN = 100_000            # Tracker AU-STREAMS: bounded, approximate

_last_recv_ts = [0.0]              # monotonic guard (single-threaded bridge)


def _next_recv_ts() -> str:
    """Strictly-increasing receive timestamp per subscriber process.

    server_received_ts doubles as part of the hypertable's UNIQUE key, so two
    entries published within one clock tick would otherwise silently lose a
    row to ignore_conflicts. Windows-clock granularity makes this REAL, not
    theoretical (observed 9 collisions in a 20-publish loop).
    فارسی: زمانِ دریافتِ اکیداً صعودی تا کلیدِ یکتا هرگز برخورد نکند.
    """
    global _last_recv_ts
    now = time.time()
    if now <= _last_recv_ts[0]:
        now = _last_recv_ts[0] + 0.000001
    _last_recv_ts[0] = now
    return repr(now)


def trim_streams(redis_client) -> None:
    """Availability, not housekeeping: with maxmemory noeviction an untrimmed
    stream eventually rejects ALL writes — including alerts. Trim on the write
    path, always (TUTORIAL §3.7). DLQ exempt: archive before trimming it.
    فارسی: هرس روی مسیر نوشتن؛ وگرنه استریمِ پُر همهٔ نوشتن‌ها را رد می‌کند.
    """
    for stream in (STREAM_REALTIME, STREAM_HISTORICAL):
        redis_client.xtrim(stream, maxlen=STREAM_MAXLEN, approximate=True)


def dedup_key(org_id: str, sensor_slot: str, device_ts: str) -> str:
    """Per-resolved-slot dedup — Blueprint §4 line 131 VERBATIM shape:
    `dedup:{org_id}:{slot}:{device_ts}` set NX EX after cache/DB resolution.
    A fully redelivered payload finds every slot key present and publishes
    nothing; genuinely new windows pass. (Multi-pack safe: each reading has
    its own key.)
    فارسی: کلید حذف تکرار به ازای هر اسلاتِ حل‌شده، عینِ اسنیپتِ کانونیکل.
    """
    return f"dedup:{org_id}:{sensor_slot}:{device_ts}"


def handle_message(redis_client, topic: str, payload: bytes, db_fetch=None) -> str | None:
    """Process one MQTT message.

    Returns None on success/duplicate, or a DLQ entry id on rejection.
    `payload` is raw bytes exactly as received — they travel intact into the
    stream entry so workers never depend on this process's parsing choices.
    """
    # Gate 1 — transport identity.
    try:
        org_id, farm_id, gateway_slot = parse_topic(topic)
    except TopicParseError:
        return send_to_dlq(redis_client, topic, payload, "bad_topic")

    try:
        data = json.loads(payload)
    except ValueError:
        return send_to_dlq(redis_client, topic, payload, "bad_json")
    if not isinstance(data, dict):
        return send_to_dlq(redis_client, topic, payload, "bad_json")

    # Gate 2 — the one mirrored field must agree with the topic.
    fault = identity_fault(gateway_slot, data)
    if fault is not None:
        return send_to_dlq(redis_client, topic, payload, fault)

    # Gate 3 — schema version gates the parser (Firmware Spec).
    if data.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        return send_to_dlq(redis_client, topic, payload, "UNSUPPORTED_SCHEMA_VERSION")

    # Gate 4 — one top-level device_ts per payload; also the dedup component.
    device_ts = data.get("device_ts")
    if not device_ts:
        return send_to_dlq(redis_client, topic, payload, "missing_device_ts")

    # Gate 5 — resolve EVERY pack first (cache-first, so this is cheap on the
    # hot path): dedup keys are PER RESOLVED SLOT (Blueprint §4:131), which
    # requires resolution before the NX check. A fully redelivered payload
    # then finds every key present and publishes nothing.
    resolved = []  # (reading, slot_id) pairs
    for reading in data.get("readings", []):
        slot_id = resolve_pack_slot(
            redis_client,
            org_id,
            data.get("gateway_hardware_id", ""),
            reading.get("pack_id"),
            db_fetch=db_fetch,
        )
        if slot_id == UNKNOWN_SLOT:
            return send_to_dlq(redis_client, topic, payload, UNKNOWN_SLOT)
        resolved.append((reading, slot_id))

    # Gate 6 — per-slot dedup; duplicates skip publishing silently.
    fresh = []
    for reading, slot_id in resolved:
        key = dedup_key(org_id, slot_id, device_ts)
        if redis_client.set(key, 1, nx=True, ex=DEDUP_TTL_SECONDS):
            fresh.append((reading, slot_id))
    if not fresh:
        return None  # full duplicate — success, nothing to do

    # Gate 7 — enrich + XADD, one entry PER READING (workers derive saloon
    # from the resolved slot). Lane by is_buffered; flat fields per decision
    # Q4 incl recv_ts + trace_id; raw JSON travels untouched. Buffered
    # publishes also bump the replay counter (Blueprint §4:139).
    lane = STREAM_HISTORICAL if data.get("is_buffered") else STREAM_REALTIME
    raw_str = payload.decode("utf-8", errors="replace")
    for reading, slot_id in fresh:
        if data.get("is_buffered"):
            redis_client.incr(f"replay:pending:{gateway_slot}")
        redis_client.xadd(
            lane,
            {
                "org_id": org_id,
                "farm_id": farm_id,
                "gateway_slot_id": gateway_slot,
                "device_ts": device_ts,
                "is_buffered": str(bool(data.get("is_buffered"))),
                "slot_id": str(slot_id),
                "payload": raw_str,
                "recv_ts": _next_recv_ts(),
                "trace_id": str(uuid.uuid4()),
                "metrics": json.dumps(reading.get("metrics", {})),
                "clock_drift": str(bool(data.get("clock_drift"))),
            },
        )

    trim_streams(redis_client)  # M3-07: bounded streams, every write
    return None
