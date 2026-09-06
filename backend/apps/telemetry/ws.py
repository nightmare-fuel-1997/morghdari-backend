"""WebSocket service layer — keys, cap, payload shape, push (M6-06).

WHAT LIVES HERE (and why): everything about the v0.6 WebSocket surface that
is NOT the consumer class itself, so both the core consumer (ws_consumer.py),
the v0.6-compat farm shim (consumers_compat.py, M6-09) and any future producer
share ONE source of truth:

- group_name / saloon_group_key: the org-scoped group name from Saas Decision
  19 (`saloon_{org_id}_{saloon_id}`) — org ids are IN the group name, so a
  consumer can never join another org's push lane by guessing saloon ids.
- active_set_key / register_connection / drop_connection: the AU-WS-CAP
  per-user connection cap as a Redis SET + TTL (TUTORIAL §3.5). A Set, never
  a counter: counters under-count after crashes (a dead socket's decrement
  never arrives), while a stale Set member only wastes one slot until the
  TTL's zombie guard expires it.
- push_telemetry_updates: the post-flush producer. The drain calls it AFTER
  its per-org commit — Channels' group_send guarantees delivery to every
  connected dashboard exactly when the rows are durably readable, which is
  the TUTORIAL's "UI follows DB" rule (never sub-second fake-realtime).

Payload shape (the message the dashboards subscribe for):
    {"type": "telemetry.update", "saloon_id": ..., "slot_id": ...,
     "device_ts": ..., "metrics": {...}}
`type` is the Channels handler dispatch key — the consumer method
telemetry_update receives it. Metrics travel VERBATIM from the stored row
(the {"temp": {"avg": ...}} window dicts the REST read API returns), so WS
and REST can never disagree about a reading's shape.

Redis client convention (consumers.py): the module reads REDIS_URL ONCE at
import; tests re-point the module-level client with a single patch. The
channel layer for group pushes is injectable the same way — unit tests bind
InMemoryChannelLayer without touching a real Redis db2.

فارسی: لایهٔ سرویس وب‌سوکت — نام گروه، سقف اتصالِ هر کاربر (مجموعهٔ ردیس با
TTL)، شکلِ پیام و تابعِ ارسالِ گروهی پس از کامیت؛ همه در یک جا تا WS و REST
هرگز دربارهٔ شکلِ داده اختلاف نظر نداشته باشند.
"""

import logging
import uuid

import redis
from channels.layers import get_channel_layer

from apps.system_config.models import SystemConfiguration

logger = logging.getLogger(__name__)

# Captured ONCE at import — test modules may point this elsewhere by patching
# (same convention as consumers.CONSUMERS_REDIS_URL).
WS_REDIS_URL = __import__("os").environ.get(
    "REDIS_URL", "redis://:dev_redis_password@localhost:6379"
)

_redis_client = None

# The cap key's zombie guard: a connection that dies without clean close()
# (wifi cut, kill -9) leaves its Set member behind; the TTL is what reclaims
# the slot. Every inbound message and every connect refreshes it.
WS_ACTIVE_TTL_SEC_DEFAULT = 300

def ws_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(WS_REDIS_URL, decode_responses=True)
    return _redis_client


def group_name(org_id, saloon_id) -> str:
    """The org-scoped group (Saas Decision 19): saloon_{org_id}_{saloon_id}.

    org_id in the NAME is the tenancy boundary for pushes — join checks in
    the consumer compare scope org_id == token org_id, and the group itself
    is unreachable from another org's consumer because the ids must match.
    """
    return f"saloon_{org_id}_{saloon_id}"


def saloon_group_key(org_id, saloon_id) -> str:
    """Backwards-compatible alias kept so callers can't fork naming."""
    return group_name(org_id, saloon_id)


def active_set_key(user_id) -> str:
    return f"ws:active:{user_id}"


def config_int(key: str, default: int) -> int:
    """D21 config-is-data read with a code fallback (same idiom as
    consumers._config_int, kept local to avoid a circular import)."""
    try:
        raw = (
            SystemConfiguration.objects.filter(key=key)
            .values_list("value", flat=True)
            .first()
        )
        return int(raw)
    except (TypeError, ValueError):
        return default


def register_connection(user_id, connection_id: str | None = None) -> str:
    """Add ONE socket to the user's ws:active Set, bump the zombie-guard TTL.

    PURE Redis — no config read, no ORM: the caller (a Channels consumer's
    connect) may run inside an event-loop executor where a DB touch would be
    a SynchronousOnlyOperation, and the cap VERDICT belongs to the consumer's
    authorize step anyway (it re-checks via active_count after registering —
    a racing over-cap socket closes itself with 4000). Never raises on Redis
    failure: cap bookkeeping must degrade, not disconnect users.
    """
    connection_id = connection_id or uuid.uuid4().hex
    try:
        client = ws_redis()
        pipe = client.pipeline()
        pipe.sadd(active_set_key(user_id), connection_id)
        pipe.expire(active_set_key(user_id), WS_ACTIVE_TTL_SEC_DEFAULT)
        pipe.execute()
    except Exception:
        logger.exception("ws:active register failed for user %s", user_id)
    return connection_id


def active_count(user_id) -> int:
    """Live cap-slot count for the user (the Set's size). Redis down → 0:
    fail-OPEN on the CAP (a degraded Redis must not lock users out) while
    every auth check stays fail-closed. The cap is storm defense, not tenancy."""
    try:
        return int(ws_redis().scard(active_set_key(user_id)))
    except Exception:
        logger.exception("ws:active scard failed for user %s", user_id)
        return 0


def drop_connection(user_id, connection_id: str) -> None:
    """Remove one socket's membership and keep the TTL honest. Missing
    member = already reclaimed by the zombie guard — a no-op, never an error."""
    try:
        client = ws_redis()
        client.srem(active_set_key(user_id), connection_id)
        client.expire(active_set_key(user_id), WS_ACTIVE_TTL_SEC_DEFAULT)
    except Exception:
        logger.exception("ws:active drop failed for user %s", user_id)


def refresh_connection_ttl(user_id) -> None:
    """The zombie-guard heartbeat: EVERY inbound message refreshes the Set's
    TTL, so a live user's cap slot survives the 300 s window indefinitely and
    only zombie slots age out. Degrades silently on Redis failure."""
    try:
        ws_redis().expire(active_set_key(user_id), WS_ACTIVE_TTL_SEC_DEFAULT)
    except Exception:
        logger.exception("ws:active TTL refresh failed for user %s", user_id)


def connection_id_in_use(user_id, connection_id: str) -> bool:
    try:
        return bool(ws_redis().sismember(active_set_key(user_id), connection_id))
    except Exception:
        return False


def telemetry_update_message(saloon_id, slot_id, device_ts, metrics, is_buffered=False) -> dict:
    """The canonical push payload. `type` is Channels' dispatch key — the
    consumer's telemetry_update() method receives it; metrics VERBATIM from
    the stored row (window dicts), matching the REST read API byte for byte.
    is_buffered rides along so compat shims (M6-09) can enforce their
    never-push-buffered rule without re-reading the hypertable."""
    return {
        "type": "telemetry.update",
        "saloon_id": saloon_id,
        "slot_id": slot_id,
        "device_ts": device_ts,
        "is_buffered": bool(is_buffered),
        "metrics": metrics,
    }


def gap_fill_message(saloon_id, rows: list[dict]) -> dict:
    """The M6-07 reconnect payload for an inbound {"action": "gap_fill"}.

    rows are ordered ascending by device_ts (the client merge is a stamp
    walk); the server holds NO session state — the client's `since` cursor
    plus this reply is the whole protocol, which is what makes the merge
    trivially testable (TUTORIAL §3.5).
    """
    return {
        "type": "telemetry.gap_fill",
        "saloon_id": saloon_id,
        "rows": rows,
    }


def push_telemetry_updates(org_id, entries, channel_layer=None) -> int:
    """Post-commit producer: one group_send per (saloon, reading).

    Called by the drain AFTER the per-org bulk_create commit — delivery is
    therefore always of DURABLE rows. Unknown-slot entries never reach the
    drain's commit batch, so they never reach here either. Returns the number
    of push dispatches (not sockets — Channels fans the group out itself).

    Channel layer is injectable for unit tests; production uses the default
    (channels_redis on db2, never the broker's db0).
    """
    layer = channel_layer or get_channel_layer()
    if layer is None:
        return 0
    dispatches = 0
    for entry in entries:
        saloon_id = entry["saloon_id"]
        for slot_id, metrics in (entry.get("metrics_by_slot") or {}).items():
            try:
                from asgiref.sync import async_to_sync

                async_to_sync(layer.group_send)(
                    group_name(org_id, saloon_id),
                    telemetry_update_message(
                        saloon_id=saloon_id,
                        slot_id=slot_id,
                        device_ts=entry["device_ts"],
                        metrics=metrics,
                        is_buffered=entry.get("is_buffered", False),
                    ),
                )
                dispatches += 1
            except Exception:
                # A dead channel layer must never fail the drain — the DB
                # commit already happened; the push is best-effort.
                logger.exception(
                    "telemetry.update push failed for org=%s saloon=%s",
                    org_id,
                    saloon_id,
                )
    return dispatches
