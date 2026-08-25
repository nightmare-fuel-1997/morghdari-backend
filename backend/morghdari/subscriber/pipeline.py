"""Per-message pipeline — the ordered gate every reading passes (M3-02+).

Order matters and is contractual:
  parse topic → identity cross-check → dedup (M3-03) → enrich/XADD (M3-04)
This module currently implements the first two gates plus resolution inputs;
dedup/enrich slots in ahead of the marked hand-off point in M3-03/M3-04.
فارسی: ترتیبِ دروازه‌ها قراردادی است — تاپیک، هویت، و بعداً حذفِ تکرار و
تغییرِ قالب؛ این ماژول فعلاً دو دروازهٔ نخست را پیاده کرده است.
"""

import json

from .dlq import send_to_dlq
from .resolver import UNKNOWN_SLOT, resolve_pack_slot
from .topics import TopicParseError, identity_fault, parse_topic

SUPPORTED_SCHEMA_VERSION = "2.0"   # Firmware Spec §3.8 — gates the parser
DEDUP_TTL_SECONDS = 90_000         # ≥ EMQX session expiry (AU-fix invariant)


def dedup_key(org_id: str, gateway_slot: str, device_ts: str) -> str:
    """Gateway-level dedup: one payload = one 60s window per gateway.

    TUTORIAL §3.3 sketches slot_id as the middle component, but payloads are
    MULTI-pack (M3-08) while device_ts is per-PAYLOAD — keying on the resolved
    pack slot would let a replayed multi-pack message partially through.
    The topic's gateway slot is stable, authenticated-adjacent, and unique per
    window. Deviation from the sketch recorded for Tracker ratification (Q4
    family).
    فارسی: کلید حذف تکرار در سطح گیت‌وی است چون هر محموله چند پک دارد ولی یک
    device_ts واحد؛ اسلاتِ تاپیک ثابت و معتبر است.
    """
    return f"dedup:{org_id}:{gateway_slot}:{device_ts}"


def handle_message(redis_client, topic: str, payload: bytes, db_fetch=None) -> str | None:
    """Process one MQTT message; returns an entry id when dead-lettered.

    Returns None on the happy path once M3-04 lands; today it also returns an
    id only for rejections. `payload` is raw bytes exactly as received.
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
        return send_to_dlq(
            redis_client, topic, payload, "UNSUPPORTED_SCHEMA_VERSION"
        )

    # Gate 4 — one top-level device_ts per payload; also the dedup component.
    device_ts = data.get("device_ts")
    if not device_ts:
        return send_to_dlq(redis_client, topic, payload, "missing_device_ts")

    # Gate 5 — CHEAP dedup BEFORE any resolution/enrichment work: QoS-1 is
    # at-least-once; this guard makes it effectively-once. Duplicates drop
    # silently (return None) — they are successes, not errors.
    key = dedup_key(org_id, gateway_slot, device_ts)
    if not redis_client.set(key, 1, nx=True, ex=DEDUP_TTL_SECONDS):
        return None

    # Per-reading slot resolution (cache-first). A miss is a DLQ reason, not a
    # crash — provisioning owns the mapping table (v0.5 writer).
    for reading in data.get("readings", []):
        pack_id = reading.get("pack_id")
        slot_id = resolve_pack_slot(
            redis_client,
            org_id,
            data.get("gateway_hardware_id", ""),
            pack_id,
            db_fetch=db_fetch,
        )
        if slot_id == UNKNOWN_SLOT:
            return send_to_dlq(redis_client, topic, payload, UNKNOWN_SLOT)

    # ── M3-04 enrich/XADD inserts here ─────────────────────────────────────
    return None
