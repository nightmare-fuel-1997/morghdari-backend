"""Provisioning lifecycle and synchronization services."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import timedelta
from typing import Any

import redis
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.devices.models import GatewaySlot
from apps.provisioning.models import ProvisioningAuditLog

logger = logging.getLogger(__name__)

STATUS_CACHE_KEY = "gateway:status:{slot_id}"
SYNC_LOCK_KEY = "lock:sync:{slot_id}"
SYNC_LOCK_TTL_SECONDS = 30

# A command the device has not answered within this window is expired by the
# sweep rather than retried forever. 10 minutes is the same TTL the bind's
# PROVISION command has always used, so one sweep policy covers both.
COMMAND_TTL_MINUTES = 10

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

        try:
            r = _redis()
            r.delete(f"gw:status:{slot.slot_id}")
            if slot.org_id:
                r.delete(f"gw:ident:{slot.org_id}:{slot.slot_id}")
                r.delete(f"gw:neg:{slot.org_id}:{slot.slot_id}")
        except Exception:
            logger.exception("failed to invalidate redis gateway cache for %s", slot.slot_id)
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


def _delete_db_acl_rules(slot) -> int:
    """Delete the gateway's DB allow-rows (postgres-authoritative brokers).

    PROD-FIX 2026-09-19 (live on VPS 82.115.17.252): production EMQX
    authorizes straight from mqtt_acl_rules (no built-in-DB chain), so
    revoking only at the broker left an already-connected gateway publishing
    indefinitely. Deleting the rows makes no_match=deny bite at the next
    authz check; the authn query already rejects the new status at CONNECT.
    Returns the deleted count.
    """
    from apps.mqtt_auth.models import MqttAclRule

    username = slot.slot_id
    n, _ = MqttAclRule.objects.filter(username=username).delete()
    if n:
        logger.info("deleted %d DB ACL rows for suspended gateway %s", n, username)
    return n


def _restore_db_acl_rules(slot) -> int:
    """Regenerate the gateway's DB allow-rows (resume path). Idempotent:
    delete-then-recreate from the canonical rule set."""
    from apps.mqtt_auth.models import MqttAclRule
    from apps.mqtt_auth.services import canonical_gateway_acl_rules

    username = slot.slot_id
    MqttAclRule.objects.filter(username=username).delete()
    # D21 fix: use canonical_gateway_acl_rules which guarantees the complete
    # union of 4 bootstrap device-lane rules + 9 tenant rules (13 total).
    items = canonical_gateway_acl_rules(username, str(slot.org_id), slot.farm_id)
    MqttAclRule.objects.bulk_create(
        [MqttAclRule(username=username, **item) for item in items]
    )
    return len(items)


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
    _delete_db_acl_rules(slot)
    try:
        discard_pending_for_gateway(_redis(), slot_id, reason="gateway_rejected")
    except Exception:
        logger.exception("pending discard failed for gateway %s", slot_id)
    return slot


def suspend_gateway(slot_id: str, by_user, *, reason: str | None = None) -> GatewaySlot:
    """Suspend a gateway's traffic without destroying its configuration: the
    EMQX ACLs are revoked (no_match=deny applies) so nothing publishes until
    resume re-syncs them. On postgres-only brokers the DB allow-rows are
    deleted instead (same deny outcome at the next authz check); the authn
    query already rejects SUSPENDED at CONNECT."""
    slot = _transition_guarded(
        slot_id, "SUSPENDED", {"PROVISIONED"},
        by_user, reason=reason or "suspended by farm manager",
    )
    from apps.mqtt_auth.services import revoke_gateway_acls

    revoke_gateway_acls(slot)
    _delete_db_acl_rules(slot)
    return slot


def resume_gateway(slot_id: str, by_user) -> GatewaySlot:
    """Resume a suspended gateway back to PROVISIONED and restore its EMQX
    allow-rules; any telemetry that was held while suspended follows the
    normal realtime/historical lanes from here on. On postgres-only brokers
    the DB allow-rows are regenerated (they were deleted at suspend)."""
    slot = _transition_guarded(
        slot_id, "PROVISIONED", {"SUSPENDED"},
        by_user, reason="farm manager resume",
    )
    from apps.mqtt_auth.services import sync_gateway_acls

    sync_gateway_acls(slot)
    _restore_db_acl_rules(slot)
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


# ---------------------------------------------------------------------------
# Command issue (Phase 3 — the half of the channel that had no writer)
# ---------------------------------------------------------------------------

def issue_command(slot_id: str, kind: str, payload: dict | None = None,
                  *, org_id=None) -> "GatewayCommand":
    """Create a command row and enqueue its publish. Returns the row.

    WHY this exists: `publish_command` and `reconcile_command_response` were
    both written and both tested, but NOTHING in the application ever called
    the publisher — the only callers were tests. The channel was therefore
    fully specified, fully reconciled, and completely dead: a device could
    answer a command it was never sent. `rotate_credential` had the same hole
    one level up — it staged a new password and returned it, but never told the
    device, so `rotation_ack` could only succeed if a human delivered the
    password by hand.

    The row is created inside the caller's transaction and the publish is
    enqueued `on_commit`, so a rolled-back request never leaves a PENDING
    command behind for the sweep to expire.
    """
    from apps.provisioning.models import GatewayCommand

    # KINDS is a plain list of strings, not choices tuples — `dict()` on it
    # raises, which is how this was caught.
    if kind not in GatewayCommand.KINDS:
        raise ValueError(f"unknown command kind {kind!r}")

    command = GatewayCommand.objects.create(
        org_id=org_id,
        gateway_slot_id=slot_id,
        kind=kind,
        payload=payload or {},
        state=GatewayCommand.STATE_PENDING,
        expires_at=timezone.now() + timedelta(minutes=COMMAND_TTL_MINUTES),
    )

    def _enqueue():
        from apps.provisioning.tasks import publish_command

        publish_command.delay(str(command.pk))

    transaction.on_commit(_enqueue)
    return command
