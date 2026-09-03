"""Provisioning lifecycle and synchronization services."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import redis
from django.core.cache import cache
from django.db import transaction

from apps.devices.models import GatewaySlot
from apps.provisioning.models import ProvisioningAuditLog

logger = logging.getLogger(__name__)

STATUS_CACHE_KEY = "gateway:status:{slot_id}"
SYNC_LOCK_KEY = "lock:sync:{slot_id}"
SYNC_LOCK_TTL_SECONDS = 30

_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379"),
            decode_responses=True,
        )
    return _redis_client


class SyncConflict(Exception):
    """Raised when another installer is synchronizing the same gateway."""


def acquire_sync_lock(slot_id: str, *, client=None) -> tuple[Any, str]:
    """Acquire a short-lived per-slot lock, returning its client and token."""
    client = client or _redis()
    token = uuid.uuid4().hex
    if not client.set(SYNC_LOCK_KEY.format(slot_id=slot_id), token, nx=True, ex=SYNC_LOCK_TTL_SECONDS):
        raise SyncConflict(slot_id)
    return client, token


def release_sync_lock(slot_id: str, token: str, *, client=None) -> None:
    """Release only our lock token; never delete a lock acquired by another request."""
    client = client or _redis()
    key = SYNC_LOCK_KEY.format(slot_id=slot_id)
    script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
    client.eval(script, 1, key, token)


def transition(
    slot_id: str,
    new_status: str,
    by_user,
    *,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> GatewaySlot:
    """Atomically transition a gateway slot and record the lifecycle event."""
    with transaction.atomic():
        slot = GatewaySlot.objects.select_for_update().get(pk=slot_id)
        previous_status = slot.provisioning_status

        if previous_status == new_status:
            return slot

        ProvisioningAuditLog.objects.create(
            gateway_slot=slot,
            org=slot.org,
            previous_status=previous_status,
            new_status=new_status,
            changed_by=by_user,
            reason=reason,
            details=details,
        )
        slot.provisioning_status = new_status
        slot.save(update_fields=["provisioning_status"])

        cache_key = STATUS_CACHE_KEY.format(slot_id=slot.slot_id)
        cache.delete(cache_key)
    return slot


# ---------------------------------------------------------------------------
# M5-05 farm-manager lifecycle orchestration (approve / reject / suspend /
# resume) — ONE service per action, each with its broker-consistency step:
# approval drains the FIFO pending buffer; rejection archives (DLQ) the held
# telemetry; suspension revokes the EMQX ACLs; resume re-syncs them.
# ---------------------------------------------------------------------------


def _transition_guarded(slot_id: str, new_status: str, allowed_from: set[str], by_user, *, reason: str | None = None):
    """Blueprint §11 state matrices: only the enumerated source statuses may
    enter `new_status`; anything else raises ValueError (the API maps it to
    409 WITHOUT a status write)."""
    current = GatewaySlot.objects.values_list("provisioning_status", flat=True).get(pk=slot_id)
    if current != new_status and current not in allowed_from:
        raise ValueError(f"cannot transition gateway {slot_id} from {current} to {new_status}")
    return transition(slot_id, new_status, by_user, reason=reason)


def approve_gateway(slot_id: str, by_user) -> GatewaySlot:
    """Approve a PENDING_APPROVAL gateway: transition to PROVISIONED, then
    drain its held telemetry FIFO into Postgres (outside the status txn so a
    drain failure never rolls the approval back) and re-sync EMQX ACLs."""
    slot = _transition_guarded(
        slot_id, "PROVISIONED", {"PENDING_APPROVAL", "PENDING_INSTALLATION"},
        by_user, reason="farm manager approval",
    )
    from apps.mqtt_auth.services import sync_gateway_acls
    from apps.telemetry.consumers import _redis, drain_stream
    from morghdari.subscriber.pipeline import STREAM_PENDING

    sync_gateway_acls(slot)
    try:
        drain_stream(STREAM_PENDING, count=1000, gateway_slot=slot_id)
    except Exception:
        logger.exception("pending drain failed for gateway %s", slot_id)
    return slot


def reject_gateway(slot_id: str, by_user, *, reason: str | None = None) -> GatewaySlot:
    """Reject a PENDING_APPROVAL gateway: transition to REJECTED, archive the
    held telemetry to the DLQ (discarded but never silently lost) and revoke
    the EMQX ACLs."""
    slot = _transition_guarded(
        slot_id, "REJECTED", {"PENDING_APPROVAL"},
        by_user, reason=reason or "rejected by farm manager",
    )
    from apps.mqtt_auth.services import revoke_gateway_acls
    from apps.telemetry.consumers import _redis, discard_pending_for_gateway

    revoke_gateway_acls(slot)
    try:
        discard_pending_for_gateway(_redis(), slot_id, reason="gateway_rejected")
    except Exception:
        logger.exception("pending discard failed for gateway %s", slot_id)
    return slot


def suspend_gateway(slot_id: str, by_user, *, reason: str | None = None) -> GatewaySlot:
    """Suspend a gateway's traffic without destroying its configuration: the
    EMQX ACLs are revoked (no_match=deny applies) so nothing publishes until
    resume re-syncs them."""
    slot = _transition_guarded(
        slot_id, "SUSPENDED", {"PROVISIONED"},
        by_user, reason=reason or "suspended by farm manager",
    )
    from apps.mqtt_auth.services import revoke_gateway_acls

    revoke_gateway_acls(slot)
    return slot


def resume_gateway(slot_id: str, by_user) -> GatewaySlot:
    """Resume a suspended gateway back to PROVISIONED and restore its EMQX
    allow-rules; any telemetry that was held while suspended follows the
    normal realtime/historical lanes from here on."""
    slot = _transition_guarded(
        slot_id, "PROVISIONED", {"SUSPENDED"},
        by_user, reason="farm manager resume",
    )
    from apps.mqtt_auth.services import sync_gateway_acls

    sync_gateway_acls(slot)
    return slot


def cached_status(slot_id: str) -> str | None:
    """Return a cached status, or populate it from the database."""
    key = STATUS_CACHE_KEY.format(slot_id=slot_id)
    status = cache.get(key)
    if status is not None:
        return status

    status = GatewaySlot.objects.filter(pk=slot_id).values_list(
        "provisioning_status", flat=True
    ).first()
    if status is not None:
        cache.set(key, status)
    return status
