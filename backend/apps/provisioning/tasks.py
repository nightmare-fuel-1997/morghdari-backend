"""Provisioning tasks — publish, reconcile, converge (Phase 0, 2026-10-18).

WHY the DB write and the MQTT publish are separate steps: the business decision
commits first, then a task pushes it. A broker outage must never roll back an
installer's submit or a support decision — the project's existing FNV-soft
convention (`apps/provisioning/services.py`). Convergence is the safety net:
the beat sweep republishes anything whose device has not caught up.

RLS note (why `admin_alias()` appears here): the device lane
`devices/{slot}/reported` deliberately carries no org segment (AD-3), so a task
holding only a slot id cannot scope its first read to a tenant. The sanctioned
pattern for cross-org DISCOVERY is the admin alias, after which every write runs
inside `tenant_context(org_id)` — the same shape BUG-9/BUG-11 established for
background sweeps (`apps/tenants/context.py::admin_alias`).
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.mqtt_auth import publisher
from apps.provisioning import topics

logger = logging.getLogger(__name__)

CONVERGENCE_BATCH = 200


class _SlotRef:
    """Minimal slot stand-in: id + org, enough for `publish_desired_state_for`."""

    __slots__ = ("slot_id", "org_id")

    def __init__(self, slot_id, org_id):
        self.slot_id = slot_id
        self.org_id = org_id


def _discover_slot(slot_id: str):
    """Read a slot's org + versions without a tenant GUC (discovery only)."""
    from apps.devices.models import GatewaySlot
    from apps.tenants.context import admin_alias

    return (
        GatewaySlot.objects.using(admin_alias())
        .filter(pk=slot_id)
        .values("slot_id", "org_id", "farm_id", "provisioning_status",
                "desired_config_version", "applied_config_version")
        .first()
    )


def publish_desired_state_for(slot, org_id=None) -> bool:
    """Publish one slot's retained configuration. Returns True on broker ACK.

    Records the outcome on the slot (`last_publish_error`) so the four-state
    view (AD-22) can show `FAILED` instead of silently pretending success.
    """
    from apps.devices.models import GatewaySlot
    from apps.provisioning.desired_state import build_desired_state
    from apps.tenants.context import tenant_context

    org_id = org_id or slot.org_id
    with transaction.atomic(), tenant_context(org_id):
        fresh = GatewaySlot.objects.select_for_update().get(pk=slot.slot_id)
        document = build_desired_state(fresh)
        ok = publisher.publish(
            topics.desired_topic(fresh.slot_id), document, retain=True
        )
        fresh.last_publish_error = None if ok else "publish failed (broker unreachable or no PUBACK)"
        fresh.save(update_fields=["last_publish_error"])
    if not ok:
        logger.warning("desired-state publish failed for slot=%s", slot.slot_id)
    return ok


@shared_task(name="provisioning.publish_desired_state")
def publish_desired_state(slot_id: str, org_id: str | None = None) -> bool:
    """Celery entry point: publish one slot's desired state (retained, QoS 1)."""
    row = _discover_slot(slot_id)
    if row is None:
        logger.warning("publish_desired_state: unknown slot %s", slot_id)
        return False
    return publish_desired_state_for(
        _SlotRef(row["slot_id"], row["org_id"]), org_id=org_id or row["org_id"]
    )


@shared_task(name="provisioning.publish_command")
def publish_command(command_uuid: str) -> bool:
    """Publish one Category A command on D3 and record the broker verdict.

    PENDING → SENT on PUBACK; PENDING stays PENDING (with last_error and an
    incremented attempt count) on failure, so the retry sweep can pick it up.
    """
    from apps.provisioning.models import GatewayCommand
    from apps.tenants.context import admin_alias, tenant_context

    row = (
        GatewayCommand.objects.using(admin_alias())
        .filter(pk=command_uuid)
        .values("command_uuid", "org_id", "gateway_slot_id", "kind", "payload", "expires_at")
        .first()
    )
    if row is None:
        logger.warning("publish_command: unknown command %s", command_uuid)
        return False
    if row["expires_at"] and row["expires_at"] <= timezone.now():
        with transaction.atomic(), tenant_context(row["org_id"]):
            GatewayCommand.objects.filter(pk=command_uuid).update(state="EXPIRED")
        return False

    envelope = {
        "command_uuid": str(row["command_uuid"]),
        "action": row["kind"],
        "params": row["payload"] or {},
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
    }
    ok = publisher.publish(topics.command_topic(row["gateway_slot_id"]), envelope)

    with transaction.atomic(), tenant_context(row["org_id"]):
        update = {"attempts": _next_attempts(row["command_uuid"])}
        if ok:
            update.update({"state": "SENT", "sent_at": timezone.now(), "last_error": None})
        else:
            update["last_error"] = "publish failed (broker unreachable or no PUBACK)"
        GatewayCommand.objects.filter(pk=command_uuid).update(**update)
    return ok


def _next_attempts(command_uuid) -> int:
    from apps.provisioning.models import GatewayCommand

    current = (
        GatewayCommand.objects.filter(pk=command_uuid)
        .values_list("attempts", flat=True)
        .first()
        or 0
    )
    return current + 1


def reconcile_reported(payload: dict, topic_slot_id: str | None = None) -> str:
    """Apply one D2 report. Returns a short outcome code (for logs and tests).

    Codes: APPLIED, FAILED, IGNORED_STALE, IDENTITY_MISMATCH, UNKNOWN_SLOT,
    MALFORMED.
    """
    if not isinstance(payload, dict):
        return "MALFORMED"
    slot_id = str(payload.get("slot_id") or "")
    if not slot_id:
        return "MALFORMED"
    # Same identity-fault rule the telemetry lane enforces: the topic segment is
    # unforgeable (the ACL pins it), the payload's mirror must agree.
    if topic_slot_id is not None and slot_id != topic_slot_id:
        return "IDENTITY_MISMATCH"

    row = _discover_slot(slot_id)
    if row is None:
        logger.warning("reconcile_reported: unknown slot %s", slot_id)
        return "UNKNOWN_SLOT"

    org_id = row["org_id"]
    try:
        reported_version = int(payload.get("config_version") or 0)
    except (TypeError, ValueError):
        return "MALFORMED"
    state = str(payload.get("state") or "").upper()

    from apps.devices.models import GatewaySlot
    from apps.provisioning.models import GatewayCommand, InstallationJob
    from apps.tenants.context import tenant_context

    with transaction.atomic(), tenant_context(org_id):
        slot = GatewaySlot.objects.select_for_update().filter(pk=slot_id).first()
        if slot is None:
            return "UNKNOWN_SLOT"
        slot.last_reported_at = timezone.now()

        if state == "FAILED":
            slot.last_publish_error = str(payload.get("error") or "device reported FAILED")
            slot.save(update_fields=["last_reported_at", "last_publish_error"])
            # 2026-10-24 (live verification): this used to fail EVERY in-flight
            # command on the slot and stamp applied_at on them. applied_at on a
            # FAILED row is a lie, and one command's failure must not fail its
            # neighbours — the ROTATE-CREDENTIAL defect chain cost a REBOOT its
            # state through exactly this path. A failed config application is
            # about the config, so only PROVISION-kind commands (the bind's own
            # first-config publish) may be failed by it.
            GatewayCommand.objects.filter(
                gateway_slot_id=slot_id, kind="PROVISION",
                state__in=["PENDING", "SENT", "ACKNOWLEDGED"],
            ).update(state="FAILED", last_error=slot.last_publish_error)
            return "FAILED"

        if state != "APPLIED":
            slot.save(update_fields=["last_reported_at"])
            return "MALFORMED"

        if reported_version < slot.applied_config_version:
            # Out-of-order or duplicated delivery: the device is behind what we
            # already recorded. Never move the counter backwards.
            slot.save(update_fields=["last_reported_at"])
            return "IGNORED_STALE"

        slot.applied_config_version = reported_version
        slot.applied_at = timezone.now()
        slot.last_publish_error = None
        slot.save(update_fields=[
            "last_reported_at", "applied_config_version", "applied_at", "last_publish_error",
        ])

        GatewayCommand.objects.filter(
            gateway_slot_id=slot_id, kind="PROVISION", state__in=["PENDING", "SENT", "ACKNOWLEDGED"]
        ).update(state="APPLIED", applied_at=timezone.now())

        _maybe_activate(slot, InstallationJob)
        return "APPLIED"


def _maybe_activate(slot, InstallationJob) -> None:
    """AD-17: PROVISIONED when the config is applied AND the job was submitted.

    This is the technical gate that replaced farm-manager approval. It lives
    here, not in a view, so no client bug can activate an unconfigured device.
    """
    from apps.accounts.system import get_system_user
    from apps.provisioning.services import transition

    if slot.provisioning_status not in ("PENDING_INSTALLATION", "PENDING_APPROVAL"):
        return
    if slot.applied_config_version < slot.desired_config_version:
        return
    job = InstallationJob.objects.filter(gateway_slot_id=slot.slot_id).first()
    if job is None or job.state != InstallationJob.STATE_SUBMITTED:
        return

    transition(
        slot.slot_id, "PROVISIONED", get_system_user(),
        reason="config applied",
        details={
            "applied_config_version": slot.applied_config_version,
            "desired_config_version": slot.desired_config_version,
            "job_id": job.pk,
        },
    )
    job.state = InstallationJob.STATE_COMPLETED
    job.completed_at = timezone.now()
    job.save(update_fields=["state", "completed_at"])


@shared_task(name="provisioning.reconcile_reported_payload")
def reconcile_reported_payload(payload: dict, topic_slot_id: str | None = None) -> str:
    """Celery wrapper so the subscriber can hand off without importing models."""
    return reconcile_reported(payload, topic_slot_id=topic_slot_id)


@shared_task(name="provisioning.sweep_config_convergence")
def sweep_config_convergence(limit: int = CONVERGENCE_BATCH) -> int:
    """Beat sweep: republish desired state where the device has not caught up.

    This is the whole retry story for AD-3. A publish that failed, or a device
    that was offline when the document was retained, converges here without any
    queue bookkeeping. Returns the number of slots republished.
    """
    from apps.devices.models import GatewaySlot
    from apps.tenants.context import admin_alias

    from django.db.models import F

    stale = list(
        GatewaySlot.objects.using(admin_alias())
        .filter(applied_config_version__lt=F("desired_config_version"))
        .exclude(provisioning_status__in=["REJECTED", "SUSPENDED"])
        .values_list("slot_id", "org_id")[:limit]
    )
    republished = 0
    for slot_id, org_id in stale:
        try:
            if publish_desired_state_for(_SlotRef(slot_id, org_id), org_id=org_id):
                republished += 1
        except Exception:
            logger.exception("convergence sweep failed for slot=%s", slot_id)
    return republished


# ---------------------------------------------------------------------------
# Command channel (Phase 3, closes D9)
# ---------------------------------------------------------------------------

@shared_task(name="provisioning.reconcile_command_response")
def reconcile_command_response(payload: dict, topic_slot_id: str | None = None) -> str:
    """Apply one D4 command-response. Returns a short outcome code.

    The command channel was fully specified in v0.5 (tasks E1-E6, Blueprint
    §11.2) and never written: `command_logs` had zero writers and nothing
    subscribed to the response lane. This is that reconciler.

    The state machine, per the Blueprint matrix:
        SENT → ACKNOWLEDGED   (device accepted the envelope)
        ACKNOWLEDGED → APPLIED (device finished; `state` in the report is SUCCESS)
        any → FAILED           (device reports an error)

    Two rules that are easy to get wrong and expensive to debug:
      * **Late SUCCESS stays APPLIED but never resurrects a FAILED row.** A
        device that retries after the sweep expired its command must not make a
        dead command look alive; we record the outcome in `last_error` instead.
      * **A response for an unknown command is IGNORED, not an error.** Devices
        may replay their outbox after a reconnect, and the row may have been
        superseded. That is normal, not a fault.
    """
    if not isinstance(payload, dict):
        return "MALFORMED"

    command_uuid = str(payload.get("command_uuid") or "").strip()
    if not command_uuid:
        return "MALFORMED"

    from apps.provisioning.models import GatewayCommand
    from apps.tenants.context import admin_alias, tenant_context

    # Discovery over the admin alias: the response arrives on a topic whose org
    # segment we trust, but the row is org-scoped and FORCE RLS'd, so a plain
    # read from an org-less consumer sees nothing.
    head = (
        GatewayCommand.objects.using(admin_alias())
        .filter(pk=command_uuid)
        .values("command_uuid", "org_id", "gateway_slot_id", "kind", "state")
        .first()
    )
    if head is None:
        logger.info("command response for unknown command %s (ignored)", command_uuid)
        return "UNKNOWN_COMMAND"
    if topic_slot_id is not None and str(head["gateway_slot_id"]) != str(topic_slot_id):
        return "IDENTITY_MISMATCH"

    state = str(payload.get("state") or "").strip().upper()
    error = payload.get("error")
    now = timezone.now()

    with transaction.atomic(), tenant_context(head["org_id"]):
        row = GatewayCommand.objects.select_for_update().filter(pk=command_uuid).first()
        if row is None:
            return "UNKNOWN_COMMAND"

        if row.state in (GatewayCommand.STATE_FAILED, GatewayCommand.STATE_EXPIRED):
            # Never resurrect. Record that the device eventually answered, so the
            # operator can see the truth without the state lying about liveness.
            row.last_error = (
                f"late {state or 'response'} after {row.state}"
                + (f": {error}" if error else "")
            )
            row.save(update_fields=["last_error"])
            return "LATE_RESPONSE"

        if state == "ACK":
            if row.state == GatewayCommand.STATE_PENDING:
                # No PUBACK recorded but the device clearly has it — trust the
                # device over our own bookkeeping.
                row.sent_at = row.sent_at or now
            row.state = GatewayCommand.STATE_ACKNOWLEDGED
            row.acked_at = now
            row.save(update_fields=["state", "acked_at", "sent_at"])
            return "ACKNOWLEDGED"

        if state in ("SUCCESS", "APPLIED"):
            row.state = GatewayCommand.STATE_APPLIED
            row.applied_at = now
            row.acked_at = row.acked_at or now
            row.last_error = None
            row.save(update_fields=["state", "applied_at", "acked_at", "last_error"])
            _on_command_applied(row, payload)
            return "APPLIED"

        if state in ("FAILED", "ERROR"):
            row.state = GatewayCommand.STATE_FAILED
            row.applied_at = now
            row.last_error = str(error or payload.get("reason") or "device reported failure")
            row.save(update_fields=["state", "applied_at", "last_error"])
            return "FAILED"

    return "MALFORMED"


def _on_command_applied(row, payload: dict) -> None:
    """Per-kind effects of a command that the device confirmed.

    Deliberately narrow: each branch is a real, idempotent action, and an
    unknown kind is a no-op rather than an error — a future kind must not be
    able to break the reconciler for every other command.
    """
    from apps.provisioning.models import GatewayCommand

    if row.kind == GatewayCommand.KIND_ROTATE:
        # Commit-on-ACK (task 3.2 / E4). The device proves it holds the new
        # password by presenting it; without that proof we do NOT promote the
        # staged hash, because a "SUCCESS" alone is not evidence the device can
        # authenticate with it.
        from apps.mqtt_auth.services import commit_pending_rotation

        # The proof lives in the action's own `result` (the device's answer
        # envelope nests it there), but firmware has put it at the top level too,
        # and rejecting a correct rotation over shape would lock a fleet out.
        # Accept either, and prefer the nested one.
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        presented = str(
            payload.get("password")
            or result.get("password")
            or ""
        )
        if not presented:
            logger.warning(
                "ROTATE_CREDENTIAL %s reported APPLIED without the new password "
                "— leaving the rotation staged (commit needs proof)", row.pk,
            )
            return
        if not commit_pending_rotation(str(row.gateway_slot_id), presented):
            logger.warning(
                "ROTATE_CREDENTIAL %s: presented password did not match the "
                "staged hash — rotation NOT committed", row.pk,
            )
        return

    if row.kind == GatewayCommand.KIND_DIAGNOSTICS:
        # Task 3.3: keep the device's report where an operator can read it.
        from apps.accounts.system import get_system_user
        from apps.auditing.models import AccountActivityLog

        AccountActivityLog.objects.create(
            actor=get_system_user(),
            org_id=row.org_id,
            action="GATEWAY_DIAGNOSTICS_REPORT",
            details={
                "gateway_slot_id": str(row.gateway_slot_id),
                "command_uuid": str(row.pk),
                "report": payload.get("result") or payload.get("diagnostics") or {},
            },
        )
        return

    # PROVISION / REBOOT / REFRESH: the state transition above is the whole
    # effect. Config convergence is driven by the reported lane, not by this.


@shared_task(name="provisioning.sweep_command_expiry")
def sweep_command_expiry(limit: int = 200) -> int:
    """Beat sweep: expire commands past `expires_at` (task 3.4).

    WHY this exists separately from the publish path: `publish_command` expires a
    command it is asked to send too late, but a command that was SENT and never
    answered has nothing to re-drive it. Without this sweep such a row sits in
    SENT forever, the UI shows a command that will never complete, and the
    attempts counter never stops looking like progress.

    Terminal states are left alone — only in-flight rows expire.
    """
    from apps.provisioning.models import GatewayCommand
    from apps.tenants.context import admin_alias, tenant_context

    now = timezone.now()
    overdue = list(
        GatewayCommand.objects.using(admin_alias())
        .filter(
            expires_at__isnull=False,
            expires_at__lt=now,
            state__in=["PENDING", "SENT", "ACKNOWLEDGED"],
            applied_at__isnull=True,  # 2026-10-24: a row that already applied must never expire
        )
        .values_list("command_uuid", "org_id")[:limit]
    )

    expired = 0
    for command_uuid, org_id in overdue:
        with transaction.atomic(), tenant_context(org_id):
            updated = GatewayCommand.objects.filter(
                pk=command_uuid, state__in=["PENDING", "SENT", "ACKNOWLEDGED"],
                applied_at__isnull=True,
            ).update(state="EXPIRED", last_error="expired before the device answered")
            expired += updated
    if expired:
        logger.info("command expiry sweep: %d command(s) expired", expired)
    return expired
