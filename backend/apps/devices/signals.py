"""Django signal handlers for GatewaySlot cache invalidation.

Connects post_save and post_delete to schedule post-commit cache invalidation
via transaction.on_commit().
"""

import logging
from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from apps.devices.cache import invalidate_gateway_slot_caches

logger = logging.getLogger(__name__)


def schedule_gateway_slot_invalidation(slot_id: str, org_id: str | None) -> None:
    """Schedule gateway slot cache invalidation upon successful transaction commit.

    If not in an active transaction, transaction.on_commit executes immediately.
    If the surrounding transaction rolls back, the callback is safely discarded.
    Deduplicates invalidation callbacks within the same transaction to prevent
    redundant post-commit operations.
    """
    if not slot_id:
        return

    s_id = str(slot_id)
    o_id = str(org_id) if org_id is not None else None

    conn = transaction.get_connection()
    if conn.in_atomic_block:
        for item in conn.run_on_commit:
            cb = item[1]
            if getattr(cb, "_gateway_invalidation", None) == (s_id, o_id):
                return

    def _do_invalidate():
        invalidate_gateway_slot_caches(s_id, o_id)

    _do_invalidate._gateway_invalidation = (s_id, o_id)
    transaction.on_commit(_do_invalidate)


@receiver(post_save, sender="devices.GatewaySlot")
def on_gateway_slot_post_save(sender, instance, **kwargs):
    """Automatic invalidation when a GatewaySlot is created or modified."""
    slot_id = getattr(instance, "slot_id", None)
    org_id = getattr(instance, "org_id", None)
    if org_id is None and getattr(instance, "org", None) is not None:
        org_id = getattr(instance.org, "pk", None)
    schedule_gateway_slot_invalidation(slot_id, org_id)


@receiver(post_delete, sender="devices.GatewaySlot")
def on_gateway_slot_post_delete(sender, instance, **kwargs):
    """Automatic invalidation when a GatewaySlot is deleted."""
    slot_id = getattr(instance, "slot_id", None)
    org_id = getattr(instance, "org_id", None)
    if org_id is None and getattr(instance, "org", None) is not None:
        org_id = getattr(instance.org, "pk", None)
    schedule_gateway_slot_invalidation(slot_id, org_id)
