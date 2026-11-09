"""Authoritative Redis and Django cache invalidation for GatewaySlot.

Maintains consistency across:
  - Option A+1 Redis subscriber metadata caches:
      gw:status:{slot_id}               (TTL 60s)
      gw:ident:{org_id}:{slot_id}       (TTL 24h)
      gw:neg:{org_id}:{slot_id}         (TTL 30s)
  - Legacy Django status cache:
      gateway:status:{slot_id}
"""

import logging
import os
import redis
from django.core.cache import cache

logger = logging.getLogger(__name__)

STATUS_CACHE_KEY = "gateway:status:{slot_id}"
_redis_client = None


def get_redis_client():
    global _redis_client
    if _redis_client is None:
        redis_url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
        _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    return _redis_client


def set_redis_client(client) -> None:
    """Set the Redis client instance (used for testing and dependency injection)."""
    global _redis_client
    _redis_client = client


def invalidate_gateway_slot_caches(slot_id: str, org_id: str | None = None, *, client=None) -> None:
    """Invalidate all authoritative subscriber Redis caches and Django status caches.

    This is the single canonical invalidation function for gateway metadata across
    the entire backend.

    Covers:
      - gw:status:{slot_id}
      - gw:ident:{org_id}:{slot_id}  (when org_id is provided)
      - gw:neg:{org_id}:{slot_id}    (when org_id is provided)
      - legacy gateway:status:{slot_id}
    """
    if not slot_id:
        return

    slot_id_str = str(slot_id)
    org_id_str = str(org_id) if org_id is not None else None

    # 1. Invalidate legacy Django cache key (M5)
    try:
        cache.delete(STATUS_CACHE_KEY.format(slot_id=slot_id_str))
    except Exception:
        logger.exception("failed to invalidate django status cache for slot %s", slot_id_str)

    # 2. Invalidate subscriber Redis keys (Option A+1)
    try:
        r = client or get_redis_client()
        keys_to_del = [f"gw:status:{slot_id_str}"]
        if org_id_str:
            keys_to_del.append(f"gw:ident:{org_id_str}:{slot_id_str}")
            keys_to_del.append(f"gw:neg:{org_id_str}:{slot_id_str}")
        r.delete(*keys_to_del)
    except Exception:
        logger.exception("failed to invalidate redis gateway cache for slot %s", slot_id_str)
