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
    """
    if not slot_id:
        return

    s_id = str(slot_id)
    o_id = str(org_id) if org_id is not None else None

    def _do_invalidate():
        invalidate_gateway_slot_caches(s_id, o_id)

    transaction.on_commit(_do_invalidate)


@receiver(post_save, sender="devices.GatewaySlot")
def on_gateway_slot_post_save(sender, instance, **kwargs):
    """Automatic invalidation when a GatewaySlot is created or modified."""
    slot_id = getattr(instance, "slot_id", None)
    org_id = getattr(instance, "org_id", None)
    schedule_gateway_slot_invalidation(slot_id, org_id)


@receiver(post_delete, sender="devices.GatewaySlot")
def on_gateway_slot_post_delete(sender, instance, **kwargs):
    """Automatic invalidation when a GatewaySlot is deleted."""
    slot_id = getattr(instance, "slot_id", None)
    org_id = getattr(instance, "org_id", None)
    schedule_gateway_slot_invalidation(slot_id, org_id)
