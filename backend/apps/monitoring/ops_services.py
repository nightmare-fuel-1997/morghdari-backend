"""Ops services — M6-10 (contract v1.7.0): pagination, filters, guards,
and the diag:last:{slot} reader (O9).

Additive-only posture: everything here READS frozen tables or the new Redis
key family; nothing alters v1.5.0 behavior. The pagination helper is written
to be shared verbatim with v1.8.0 (spec §3).

Redis-down degradation (D22): a failing diag read returns None — O9 then
answers 404 "no diagnostics yet". A missing measurement is reported as
missing, never fabricated.

فارسی: صفحه‌بندیِ استانداردِ جدید، نگهبانِ بازهٔ آستانه‌ها، و خوانندهٔ آخرین
گزارشِ خودِ گیت‌وی — همه فقط خواندنی؛ خرابیِ ردیس یعنی «گزارشی نیست»، نه صفرِ ساختگی.
"""

import json
import logging
import os

from django.core.exceptions import BadRequest

from morghdari.subscriber.diagnostics import DIAG_PREFIX

logger = logging.getLogger(__name__)

# O3 sanity bounds — metric-keyed (metric, min, max). The (min < max)
# cross-field rule is metric-agnostic and always enforced. Decimals are
# bounded to the column's precision (max_digits=6, decimal_places=2).
THRESHOLD_BOUNDS = {
    "temp": (-40, 80),
    "humidity": (0, 100),
    "lux": (0, 200000),
    "nh3": (0, 100),
    "co2": (0, 10000),
}

# O10 common-password denylist — deliberately TINY (spec: "checked against a
# common-password denylist"); the full CommonPasswordValidator list is
# 20k+ entries and DRF validation errors would leak its shape anyway.
COMMON_PASSWORDS = {
    "password", "password1", "password12", "password123", "12345678",
    "123456789", "1234567890", "qwerty123", "qwertyui", "letmein1",
    "morghdari", "poultry1", "chicken1", "admin123", "welcome1",
}
MIN_PASSWORD_LENGTH = 10  # spec §2 O10: "new ≥10 chars"


# ---------------------------------------------------------------------------
# Pagination (spec §1 — the v1.7.0+ convention)
# ---------------------------------------------------------------------------


class InvalidPagination(BadRequest):
    """Raised for negative/non-numeric limit/offset; view maps to 400."""


def parse_pagination(request) -> tuple[int, int]:
    """(limit, offset) with spec §1 defaults and validation.

    Raises InvalidPagination on negative/non-numeric values — the view
    translates that to `400 {"error": "invalid pagination"}`.
    """
    raw_limit = request.query_params.get("limit", "50")
    raw_offset = request.query_params.get("offset", "0")
    try:
        limit = int(raw_limit)
        offset = int(raw_offset)
    except (TypeError, ValueError):
        raise InvalidPagination()
    if limit < 0 or offset < 0:
        raise InvalidPagination()
    return min(limit, 500), offset


def page(request, qs):
    """Apply limit/offset to a QuerySet; returns the sliced page."""
    limit, offset = parse_pagination(request)
    return qs[offset : offset + limit]


def paginated(request, items, count=None) -> dict:
    """Slice an already-materialized list into the {results, count} envelope."""
    limit, offset = parse_pagination(request)
    window = items[offset : offset + limit]
    return {"results": window, "count": len(items) if count is None else count}


# ---------------------------------------------------------------------------
# O1 — audit log filters (RLS-scoped query builder)
# ---------------------------------------------------------------------------


def parse_iso_z(raw: str):
    """Parse `2026-09-06T12:00:00Z` (space also tolerated); None if invalid."""
    from datetime import datetime

    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_audit_filters(qs, request, org):
    """Apply O1's optional filters to an org-scoped AccountActivityLog QS."""
    actor_id = request.query_params.get("actor_id")
    if actor_id:
        try:
            qs = qs.filter(actor_id=int(actor_id))
        except (TypeError, ValueError):
            raise InvalidPagination()  # spec: bad scalar ⇒ 400 envelope
    action = request.query_params.get("action")
    if action:
        qs = qs.filter(action=action)
    farm_id = request.query_params.get("farm_id")
    if farm_id:
        try:  # Farm PK is an INT AutoField — validate as int, never UUID
            int(farm_id)
        except (TypeError, ValueError):
            raise InvalidPagination()  # malformed scalar ⇒ 400, never a 500
        qs = qs.filter(farm_id=int(farm_id))
    saloon_id = request.query_params.get("saloon_id")
    if saloon_id:
        try:
            int(saloon_id)
        except (TypeError, ValueError):
            raise InvalidPagination()
        qs = qs.filter(saloon_id=saloon_id)
    for param, lookup in (("from", "timestamp__gte"), ("to", "timestamp__lte")):
        raw = request.query_params.get(param)
        if raw:
            parsed = parse_iso_z(raw)
            if parsed is None:
                # Bad scalar ⇒ the 400 envelope (same posture as bad
                # actor_id/farm_id — spec §4's only 400 classes plus scalars).
                raise InvalidPagination()
            qs = qs.filter(**{lookup: parsed})
    return qs


# ---------------------------------------------------------------------------
# O3 — threshold range guard
# ---------------------------------------------------------------------------


def threshold_range_error(metric: str, payload: dict) -> str | None:
    """O3 validation: an error string to 400 with, or None when clean.

    Rules: known metric key; decimals within metric bounds; min < max when
    both present; `enabled` boolean when present. Absent keys pass (partial
    PUT is contractual).
    """
    lo, hi = THRESHOLD_BOUNDS.get(metric, (-10**6, 10**6))
    for side, bound in (("min_value", lo), ("max_value", hi)):
        if side in payload and payload[side] is not None:
            try:
                value = float(payload[side])
            except (TypeError, ValueError):
                return "threshold out of range"
            if not (bound <= value <= hi if side == "min_value" else lo <= value <= bound):
                return "threshold out of range"
    if payload.get("min_value") is not None and payload.get("max_value") is not None:
        if float(payload["min_value"]) >= float(payload["max_value"]):
            return "threshold out of range"
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        return "threshold out of range"
    return None


# ---------------------------------------------------------------------------
# O6 — photo file validation
# ---------------------------------------------------------------------------

MAX_PHOTO_BYTES = 10 * 1024 * 1024  # spec §2 O6: ≤10 MB
ALLOWED_PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}


def photo_error(uploaded) -> str | None:
    """O6 validation: error string to 400 with, or None when acceptable."""
    if uploaded is None:
        return "file required"
    if uploaded.size > MAX_PHOTO_BYTES:
        return "file too large (max 10 MB)"
    if (uploaded.content_type or "") not in ALLOWED_PHOTO_TYPES:
        return "unsupported file type (jpg/png only)"
    return None


# ---------------------------------------------------------------------------
# O9 — diag:last:{slot} reader
# ---------------------------------------------------------------------------


def _redis_client():
    """Fresh short-timeout client per read — same convention as
    monitoring.views._redis / telemetry.ws (module reads REDIS_URL)."""
    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True, socket_timeout=0.6)


def read_last_diagnostics(gateway_slot: str) -> dict | None:
    """O9's store read: the retained diag:last hash, or None when the
    gateway never reported (or Redis is down — D22 honest degradation)."""
    client = _redis_client()
    try:
        raw = client.hgetall(f"{DIAG_PREFIX}{gateway_slot}")
    except Exception:  # noqa: BLE001
        logger.warning("diag read failed for %s", gateway_slot, exc_info=True)
        return None
    if not raw:
        return None
    return {
        "slot_id": gateway_slot,
        "reported_at": raw.get("reported_at") or None,
        "battery_v": _as_float(raw.get("battery_v")),
        "rssi": _as_float(raw.get("rssi")),
        "free_heap": _as_float(raw.get("free_heap")),
        "sd_ok": _as_bool(raw.get("sd_ok")),
        "errors": _as_error_list(raw.get("errors")),
    }


def _as_float(raw):
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _as_bool(raw):
    if raw is None or raw == "":
        return None
    return raw == "1"


def _as_error_list(raw):
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
