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


def handle_message(redis_client, topic: str, payload: bytes, db_fetch=None) -> str | None:
    """Process one MQTT message; returns an entry id when dead-lettered.

    Returns None on the happy path once M3-04 lands; today it also returns an
    id only for rejections. `payload` is raw bytes exactly as received.
    """
    # Gate 1 — transport identity.
    try:
        org_id, farm_id, gateway_slot = parse_topic(topic)
    except TopicParseError as exc:
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
            return send_to_dlq(
                redis_client, topic, payload, UNKNOWN_SLOT
            )

    # ── M3-03 dedup + M3-04 enrich/XADD insert here ────────────────────────
    return None
