"""Last-diagnostics retention — M6-10 (contract v1.7.0 O9, new `diag:last:{slot}`).

Firmware Spec §3.8 allows a payload whose `readings` list is EMPTY while
`diagnostics` is present — the gateway's self-report (battery, RSSI, free
heap, SD health). Before this module such payloads flowed through the
pipeline and died silently at Gate 5 (nothing to resolve/publish); the
evidence they carried was unreachable by any operator.

Additive rule honored: telemetry handling is UNTOUCHED — non-empty readings
take exactly the pre-existing path. This module only (a) captures the
self-report of diagnostics-only payloads into the `diag:last:{slot}` Redis
hash (spec §7's new key family) and (b) returns a SUCCESS verdict for them so
the DLQ never sees a healthy self-report (O9 serves the retained record with
404 only when the gateway NEVER reported).

Store shape (hash, one per gateway):
  reported_at  — the payload's device_ts verbatim (gateway's clock)
  received_at  — subscriber receipt (unix seconds, for O9's ordering)
  battery_v / rssi / free_heap / sd_ok — numeric/bool passthrough (missing
                 firmware keys stay absent; the reader returns nulls)
  errors       — JSON-encoded list of error strings ([] when none)

فارسی: گزارشِ خودِ گیت‌وی (باتری، آنتن، حافظه) که تا امروز بی‌صدا هدر می‌رفت،
حالا در هشِ diag:last:{slot} نگه داشته می‌شود تا O9 بخواند — مسیرِ تله‌متری
دست‌نخورده، فقط افزوده.
"""

import json
import time

DIAG_PREFIX = "diag:last:"  # spec §7 — new key family, additive


def _coerce_bool(raw):
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("ok", "true", "1", "yes"):
            return True
        if lowered in ("fail", "failed", "false", "0", "no", "error"):
            return False
    if isinstance(raw, (int, float)):
        return bool(raw)
    return None  # absent/unparseable — the reader reports null, not a guess


def capture_diag(redis_client, gateway_slot: str, data: dict) -> None:
    """HSET the self-report fields onto diag:last:{slot} (best-effort).

    Called from handle_message between identity and schema gates — BEFORE
    Gate 5 — so an empty-readings payload is retained even though nothing
    publishes. Any Redis/shape failure is swallowed with a warning: a lost
    diagnostic must never take the telemetry pipeline down (same degradation
    posture as the DLQ writer).
    """
    try:
        diag = data.get("diagnostics")
        if not isinstance(diag, dict):
            return  # not a self-report; nothing to retain
        errors = diag.get("errors", [])
        if not isinstance(errors, list):
            errors = []
        mapping = {
            "reported_at": str(data.get("device_ts") or ""),
            "received_at": str(int(time.time())),
            "errors": json.dumps([str(e) for e in errors]),
        }
        for key in ("battery_v", "rssi", "free_heap"):
            if diag.get(key) is not None:
                mapping[key] = str(diag[key])
        sd_ok = _coerce_bool(diag.get("sd_ok"))
        if sd_ok is not None:
            mapping["sd_ok"] = "1" if sd_ok else "0"
        redis_client.hset(f"{DIAG_PREFIX}{gateway_slot}", mapping=mapping)
    except Exception:  # noqa: BLE001 — retention must never crash the pipe
        import logging

        logging.getLogger(__name__).warning(
            "diag capture failed for %s", gateway_slot, exc_info=True
        )


def is_diag_only(data: dict) -> bool:
    """True when the payload carries a diagnostics self-report and NOTHING
    publishable — exactly the Firmware §3.8 shape that used to die at Gate 5."""
    return bool(data.get("diagnostics")) and not data.get("readings")
