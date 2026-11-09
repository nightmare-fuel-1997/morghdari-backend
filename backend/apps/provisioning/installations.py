"""Installation jobs — assignment, the installer queue, and the job session.

WHY these live here and not under `/platform/support/` (design doc §5.2,
contradiction X1): a Device Installer is org-less platform staff of the software
company. Every `/platform/support/*` endpoint demands an org-scoped support
session (`accounts/views.py::_support_op_context`), which an installer cannot
obtain — there is no org to scope to until support assigns them a job. So the
installer's own surface is org-less, and the org appears only inside the token
that `session/` mints (middleware branch ①b, AD-14).

RLS consequence, stated once: `installation_jobs` is org-scoped, so an org-less
installer reading their queue would see zero rows under FORCE RLS. Every read
therefore DISCOVERS its org over the admin alias first and then runs inside
`tenant_context(org)` — the pattern `subscriber/main.py` established. Writes are
always inside the tenant context, never over the alias.
"""

from __future__ import annotations

import logging

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.platform_gate import staff_denied
from apps.accounts.session_tokens import (
    KIND_INSTALLER,
    SESSION_TTL_SEC,
    issue_scoped_session,
    session_expiry_iso,
)
from apps.accounts.models import AccountRoleScope, OrgMember
from apps.auditing.models import AccountActivityLog, PlatformAuditLog
from apps.mqtt_auth.services import verify_password
from apps.provisioning.models import InstallationJob
from apps.tenants.context import admin_alias, tenant_context

logger = logging.getLogger(__name__)

TAG = "platform-installations"

# Who may see the queue at all: installers see their own, support/admin see all.
QUEUE_ROLES = frozenset({"installer", "hardware", "admin", "support"})
# Who may ASSIGN a job: the support desk. Not installers (someone else must not
# be able to hand work to themselves) and not the Hardware Team (they register
# units; they do not dispatch field crews).
ASSIGN_ROLES = frozenset({"support", "admin"})

OPEN_STATES = ("PENDING", "ASSIGNED", "BOUND", "SUBMITTED")


# ---------------------------------------------------------------------------
# RLS-aware discovery
# ---------------------------------------------------------------------------

def _claims(request) -> dict:
    return getattr(request, "jwt_claims", {}) or {}


def _is_orgless_staff(request) -> bool:
    """Platform staff with no org claim — the installer/support case."""
    user = request.user
    return bool(
        user
        and user.is_authenticated
        and user.is_platform_staff
        and not _claims(request).get("org_id")
    )


def _org_ids_for(request) -> list[str]:
    """Orgs this caller may see jobs in.

    An org-less platform caller sees jobs in every org (the queue is a
    dispatcher surface). A caller carrying an org claim is limited to it.

    MUST read over the admin alias: `organizations` is FORCE RLS like every
    other org-scoped table, so an org-less caller — who by definition has no
    GUC set — would otherwise get ZERO rows and the queue would silently show
    an empty world. This is the same masking class as D15 (review approvals on
    the VPS) and it is invisible under sqlite, where RLS does not exist.
    """
    if _is_orgless_staff(request):
        from apps.tenants.models import Organization

        return [
            str(row)
            for row in Organization.objects.using(admin_alias())
            .values_list("org_id", flat=True)
        ]
    org_id = _claims(request).get("org_id")
    return [str(org_id)] if org_id else []


def _job_queryset_for(request):
    """A queryset restricted to the caller's visible jobs, or None if cross-org."""
    if _is_orgless_staff(request):
        return None  # every org; the loop below narrows per tenant context
    org_id = _claims(request).get("org_id")
    user = request.user
    # An org member who is not platform staff sees jobs in their own org only.
    if org_id:
        return InstallationJob.objects.filter(org_id=org_id)
    return InstallationJob.objects.filter(assigned_installer=user)


def _mine_only(request) -> bool:
    """An installer sees their own queue; support/admin/hardware see everything.

    A field installer with the whole org's queue could read every farm they do
    not work for, which is exactly the over-share AD-14's scoped session exists
    to prevent.
    """
    user = request.user
    return getattr(user, "staff_role", None) == "installer" and not (
        user.is_superuser
        or (user.is_platform_staff and getattr(user, "staff_role", None) in ASSIGN_ROLES)
    )


def _iter_jobs(request, state_filter=None):
    """Yield jobs visible to the caller, each read inside its org's context."""
    seen = set()
    mine_only = _mine_only(request)

    def _rows(queryset):
        qs = queryset
        if state_filter:
            qs = qs.filter(state=state_filter)
        if mine_only:
            qs = qs.filter(assigned_installer_id=request.user.pk)
        return list(
            qs.select_related("farm", "farm__province", "unit", "gateway_slot")
            .order_by("-created_at")[:200]
        )

    if not _is_orgless_staff(request):
        for job in _rows(_job_queryset_for(request)):
            yield job
        return

    for org_id in _org_ids_for(request):
        with transaction.atomic(), tenant_context(org_id):
            try:
                rows = _rows(InstallationJob.objects.filter(org_id=org_id))
            except Exception:  # noqa: BLE001 — an unreadable org must not break the queue
                logger.exception("job discovery failed for org=%s", org_id)
                continue
            for job in rows:
                if job.pk not in seen:
                    seen.add(job.pk)
                    yield job


def _job_out(job: InstallationJob) -> dict:
    farm = job.farm
    slot = job.gateway_slot
    return {
        "id": job.pk,
        "kind": job.kind,
        "state": job.state,
        "org_id": str(job.org_id),
        "farm_id": job.farm_id,
        "farm_name": farm.name if farm_id_safe(farm) else None,
        "province_id": farm.province_id if farm_id_safe(farm) else None,
        "assigned_installer_id": job.assigned_installer_id,
        "assigned_installer_name": (
            job.assigned_installer.get_full_name() or job.assigned_installer.username
            if job.assigned_installer_id else None
        ),
        "assigned_at": job.assigned_at.isoformat() if job.assigned_at else None,
        "assigned_by_id": job.assigned_by_id,
        "reassigned_after_bind": job.reassigned_after_bind,
        "unit_id": job.unit_id,
        "gateway_slot_id": job.gateway_slot_id,
        "gateway_slot_state": slot.provisioning_status if slot else None,
        "gateway_state_view": _gateway_state_view(slot) if slot else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "notes": job.notes,
    }


def farm_id_safe(farm) -> bool:
    return farm is not None and farm.pk is not None


def _gateway_state_view(slot) -> str:
    """AD-22, derived — never stored, so it cannot disagree with the counters.

    Four states, in precedence order:
      FAILED       — a publish or a device report went wrong (`last_publish_error`)
      PENDING_SYNC — the device has never been sent a configuration
      STALLED      — sent, but the device has not caught up to it
      READY        — applied == desired (PROVISIONED once the job is submitted)
    Anything outside the install lifecycle reports its status verbatim.
    """
    if slot.last_publish_error:
        return "FAILED"
    if slot.provisioning_status not in ("PENDING_INSTALLATION", "PENDING_APPROVAL"):
        return slot.provisioning_status
    if slot.desired_config_version == 0:
        return "PENDING_SYNC"
    if slot.applied_config_version < slot.desired_config_version:
        return "STALLED"
    return "READY"


# ---------------------------------------------------------------------------
# GET /provisioning/installations/
# ---------------------------------------------------------------------------

@extend_schema(
    tags=[TAG],
    operation_id="install_jobs",
    summary="Installation queue (installer; support/admin see all)",
    description="""Newest first, capped at 200. `?state=` filters to PENDING,
ASSIGNED, BOUND, SUBMITTED, COMPLETED or CANCELLED.

An installer sees the jobs assigned to THEM; support and admin see every job.
The queue is deliberately org-less: the installer is platform staff and has no
org claim until `session/` mints one (AD-14).""",
    parameters=[
        OpenApiParameter(
            "state", str, OpenApiParameter.QUERY, required=False,
            description="PENDING | ASSIGNED | BOUND | SUBMITTED | COMPLETED | CANCELLED",
        )
    ],
    responses={200: None, 403: None},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def install_jobs(request):
    denied = staff_denied(request, QUEUE_ROLES)
    if denied is not None:
        return denied

    state = str(request.query_params.get("state") or "").strip().upper()
    if state and state not in {s for s in InstallationJob.STATES}:
        return Response({"error": "invalid state filter"}, status=400)

    jobs = list(_iter_jobs(request, state_filter=state or None))
    return Response({"count": len(jobs), "results": [_job_out(j) for j in jobs]})


# ---------------------------------------------------------------------------
# GET /provisioning/installations/{job_id}/
# ---------------------------------------------------------------------------

@extend_schema(
    tags=[TAG],
    operation_id="install_job_detail",
    summary="One installation job",
    responses={200: None, 403: None, 404: None},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def install_job_detail(request, job_id: int):
    denied = staff_denied(request, QUEUE_ROLES)
    if denied is not None:
        return denied
    job = _find_job(request, job_id)
    if job is None:
        return Response({"error": "job not found"}, status=404)
    return Response(_job_out(job))


def _find_job(request, job_id: int):
    """Locate a job the caller may see, reading it inside its org's context."""
    if not _is_orgless_staff(request):
        qs = _job_queryset_for(request)
        return qs.filter(pk=job_id).select_related("farm", "gateway_slot", "unit").first()
    from apps.tenants.models import Organization

    for org_id in Organization.objects.using(admin_alias()).values_list("org_id", flat=True):
        with transaction.atomic(), tenant_context(str(org_id)):
            job = (
                InstallationJob.objects.filter(pk=job_id)
                .select_related("farm", "gateway_slot", "unit")
                .first()
            )
        if job is not None:
            return job
    return None


# ---------------------------------------------------------------------------
# GET /provisioning/installations/assignable/
# ---------------------------------------------------------------------------

@extend_schema(
    tags=[TAG],
    operation_id="install_assignable",
    summary="Installer picker for a job (support/admin)",
    description="""`?province_id=` narrows the list using `account_role_scopes`
(the regional installer scoping from Tracker §4i): an installer whose scope has
no province is unrestricted within the org; one scoped to a province sees only
farms in it. Only ACTIVE org members with the `installer` role are returned.""",
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.QUERY, required=True),
        OpenApiParameter("province_id", int, OpenApiParameter.QUERY, required=False),
    ],
    responses={200: None, 403: None},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def install_assignable(request):
    denied = staff_denied(request, ASSIGN_ROLES)
    if denied is not None:
        return denied

    org_id = str(request.query_params.get("org_id") or "").strip()
    if not org_id:
        return Response({"error": "org_id required"}, status=400)
    # 2026-10-24 defect (live verification): a non-UUID org_id reached the
    # tenant_context UUID parser and surfaced as a raw 500. A malformed
    # caller input is a 400, never a server error.
    from uuid import UUID as _UUID

    try:
        _UUID(org_id)
    except ValueError:
        return Response({"error": "org_id must be a UUID"}, status=400)
    province_id = request.query_params.get("province_id")

    with transaction.atomic(), tenant_context(org_id):
        members = OrgMember.objects.filter(org_id=org_id, role="installer", is_active=True)
        user_ids = list(members.values_list("user", flat=True))
        scoped = {
            row["user"]: row["province"]
            for row in (
                AccountRoleScope.objects.filter(
                    org_id=org_id, user__in=user_ids, role="installer"
                ).values("user", "province")
            )
        }

    from django.contrib.auth import get_user_model

    users = get_user_model().objects.filter(pk__in=user_ids).order_by("username")
    results = []
    for user in users:
        scope = scoped.get(user.pk)
        if scope and province_id and int(province_id) != scope:
            continue
        results.append(
            {
                "user_id": user.pk,
                "username": user.username,
                "name": user.get_full_name() or user.username,
                "province_id": scope,
                "regionally_scoped": scope is not None,
            }
        )
    return Response({"count": len(results), "results": results})


# ---------------------------------------------------------------------------
# POST /provisioning/installations/{job_id}/assign/
# ---------------------------------------------------------------------------

@extend_schema(
    tags=[TAG],
    operation_id="install_job_assign",
    summary="Assign a job to an installer (support/admin)",
    description="""Support action, audited in `platform_audit_log` because the actor
is org-less. Moves PENDING → ASSIGNED.

Reassignment is allowed after the unit is bound, but it is FLAGGED
(`reassigned_after_bind`) rather than silently rewriting the assignee, so the
job history stays readable (AD-21). A COMPLETED or CANCELLED job is a 409.""",
    request=None,
    responses={200: None, 400: None, 403: None, 404: None, 409: None},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def install_job_assign(request, job_id: int):
    denied = staff_denied(request, ASSIGN_ROLES)
    if denied is not None:
        return denied

    installer_id = request.data.get("installer_id")
    reason = str(request.data.get("reason") or "").strip()
    if not installer_id:
        return Response({"error": "installer_id required"}, status=400)

    job = _find_job(request, job_id)
    if job is None:
        return Response({"error": "job not found"}, status=404)
    if job.state in (InstallationJob.STATE_COMPLETED, InstallationJob.STATE_CANCELLED):
        return Response({"error": f"job is {job.state}"}, status=409)

    org_id = str(job.org_id)
    with transaction.atomic(), tenant_context(org_id):
        member = OrgMember.objects.filter(
            org_id=org_id, user_id=installer_id, role="installer", is_active=True
        ).first()
        if member is None:
            return Response({"error": "installer is not an active member of this org"}, status=400)

        was_bound = job.state == InstallationJob.STATE_BOUND
        job.assigned_installer_id = installer_id
        job.assigned_by = request.user
        job.assigned_at = timezone.now()
        job.reassigned_after_bind = job.reassigned_after_bind or was_bound
        if job.state == InstallationJob.STATE_PENDING:
            job.state = InstallationJob.STATE_ASSIGNED
        job.save(
            update_fields=[
                "assigned_installer", "assigned_by", "assigned_at",
                "reassigned_after_bind", "state",
            ]
        )

        PlatformAuditLog.objects.create(
            actor=request.user,
            action="PLATFORM_JOB_ASSIGN",
            target_type="installation_job",
            target_id=str(job.pk),
            ip_address=request.META.get("REMOTE_ADDR"),
            details={
                "installer_id": installer_id,
                "reassigned_after_bind": job.reassigned_after_bind,
                "reason": reason,
            },
        )
        AccountActivityLog.objects.create(
            actor=request.user,
            org=job.org,
            action="INSTALLER_JOB_ASSIGN",
            details={"job_id": job.pk, "installer_id": installer_id, "reason": reason},
        )
    return Response(_job_out(_find_job(request, job_id)))


# ---------------------------------------------------------------------------
# POST /provisioning/installations/{job_id}/session/
# ---------------------------------------------------------------------------

@extend_schema(
    tags=[TAG],
    operation_id="install_job_session",
    summary="Open a scoped installer session for one job (AD-14)",
    description="""Mints a 15-minute access token whose claims add `org_id`,
`installer_session: true`, `job_id` and `farm_id`. While that token is used,
the tenant middleware sets the org GUC and RLS scopes every query to that org.

Only the job's assignee (or support/admin) may open one. The claim is
deliberately NOT `support_session`: an installer must never inherit a support
operator's powers.""",
    request=None,
    responses={200: None, 403: None, 404: None, 409: None},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def install_job_session(request, job_id: int):
    denied = staff_denied(request, QUEUE_ROLES)
    if denied is not None:
        return denied

    job = _find_job(request, job_id)
    if job is None:
        return Response({"error": "job not found"}, status=404)
    if job.state in (InstallationJob.STATE_COMPLETED, InstallationJob.STATE_CANCELLED):
        return Response({"error": f"job is {job.state}"}, status=409)

    user = request.user
    # An unassigned job has no org to scope a session to — that is a state
    # problem (409), not a permissions one.
    if job.assigned_installer_id is None:
        return Response({"error": "job is not assigned"}, status=409)
    is_assignee = job.assigned_installer_id == user.pk
    is_support = user.is_superuser or (
        user.is_platform_staff
        and getattr(user, "staff_role", None) in ASSIGN_ROLES
    )
    if not is_assignee and not is_support:
        return Response({"error": "only the job's assignee may open a session"}, status=403)

    org_id = str(job.org_id)
    token = issue_scoped_session(
        user, org_id=org_id, kind=KIND_INSTALLER,
        extra_claims={"job_id": job.pk, "farm_id": job.farm_id},
    )
    with transaction.atomic(), tenant_context(org_id):
        AccountActivityLog.objects.create(
            actor=user,
            org=job.org,
            action="INSTALLER_SESSION_OPEN",
            details={"job_id": job.pk, "farm_id": job.farm_id, "installer_id": user.pk},
        )
    return Response(
        {
            "access_token": token,
            "token_type": "Bearer",
            "org_id": org_id,
            "farm_id": job.farm_id,
            "job_id": job.pk,
            "expires_in_sec": SESSION_TTL_SEC,
            "expires_at": session_expiry_iso(),
        }
    )


# ---------------------------------------------------------------------------
# POST /provisioning/installations/{job_id}/gateway/  — the bind (AD-1/AD-3)
# ---------------------------------------------------------------------------

def _bind_job_to_unit(job, unit, *, actor, via_override: bool, reason: str, ip=None):
    """Create the slot, expand the ACL, consume the claim code. ONE transaction.

    This is the moment the two halves of the redesign meet: the org-less factory
    unit (Phase 1) and the org-scoped installation job (Phase 2 slice a/b). The
    installer scans the QR label on the physical device, and from here on the
    device has an org, a farm, and the full T1–T9 tenant ACL set.

    Everything is server-side, so it works with the unit OFFLINE — no broker
    round-trip, no "device must be on to be bound". The device learns its
    configuration the next time it connects, through the retained `desired`
    topic, which is the whole reason the doc is retained (AD-3).

    Claim-code semantics:
      * single use — `claimed_at` is stamped inside the row lock, so two
        installers racing on the same label cannot both win;
      * NOT the password — consuming it does not touch the broker credential,
        so a photographed label can still be revoked by `retire`.
    """
    from apps.devices.models import GatewaySlot
    from apps.mqtt_auth.models import MqttAclRule
    from apps.mqtt_auth.services import ensure_gateway_user
    from apps.provisioning.api import gateway_acl_rule_items
    from apps.provisioning.models import GatewayCommand

    slot_id = unit.unit_id  # AD-2: the unit id IS the slot id

    slot, slot_created = GatewaySlot.objects.get_or_create(
        slot_id=slot_id,
        defaults={
            "org": job.org,
            "farm": job.farm,
            "request": job.request,
            "created_by": actor,
        },
    )
    if slot_created:
        slot.provisioning_status = "PENDING_INSTALLATION"
        slot.save(update_fields=["provisioning_status"])

    # Expand the ACL to the full canonical set (4 device + 9 tenant). Written
    # as get_or_create so a replayed bind cannot raise on the unique index.
    from apps.mqtt_auth.services import canonical_gateway_acl_rules

    rules = canonical_gateway_acl_rules(slot_id, str(job.org_id), job.farm_id)
    for rule in rules:
        MqttAclRule.objects.get_or_create(
            username=slot_id, topic=rule["topic"],
            defaults={"permission": rule["permission"], "action": rule["action"]},
        )

    # Invalidate subscriber Redis cache so new identity and status are immediate
    try:
        from apps.provisioning.services import _redis
        r = _redis()
        r.delete(f"gw:status:{slot_id}")
        r.delete(f"gw:ident:{job.org_id}:{slot_id}")
        r.delete(f"gw:neg:{job.org_id}:{slot_id}")
    except Exception:
        pass

    # AD-4: bump the desired version so the device treats the next document as
    # NEW. Without the bump, a unit that already applied config_version 1 would
    # refuse the identical retained copy as stale.
    slot.desired_config_version += 1
    slot.save(update_fields=["desired_config_version"])

    # The PROVISION command (AD-5): the record that the device must be told its
    # configuration. It is queued here and published by the task, so the bind
    # itself never talks to a broker. `kind` is the discriminator — there is no
    # separate `action` column on this model.
    GatewayCommand.objects.create(
        org=job.org,
        gateway_slot_id=slot_id,
        job=job,
        kind="PROVISION",
        payload={"config_version": slot.desired_config_version},
        state="PENDING",
        expires_at=timezone.now() + timedelta(minutes=10),
    )

    unit.claimed_at = timezone.now()
    unit.bound_at = timezone.now()
    unit.state = "BOUND"
    unit.slot = slot
    unit.save(update_fields=["claimed_at", "bound_at", "state", "slot"])

    job.unit = unit
    job.gateway_slot = slot
    job.state = InstallationJob.STATE_BOUND
    job.save(update_fields=["unit", "gateway_slot", "state"])

    # Push the retained document AFTER the commit: a device that receives the
    # configuration before the slot exists would be told to apply state we have
    # not yet recorded. on_commit also means a test without
    # captureOnCommitCallbacks simply skips it, rather than failing.
    def _publish():
        from apps.provisioning.tasks import publish_desired_state_for

        try:
            publish_desired_state_for(slot)
        except Exception:  # noqa: BLE001 — convergence sweep is the safety net
            logger.exception("desired-state publish failed after binding %s", slot_id)

    transaction.on_commit(_publish)

    PlatformAuditLog.objects.create(
        actor=actor, action="PLATFORM_JOB_BIND", target_type="installation_job",
        target_id=str(job.pk), ip_address=ip,
        details={
            "unit_id": unit.unit_id, "slot_id": slot_id,
            "config_version": slot.desired_config_version,
            "via_override": via_override, "reason": reason,
            "acl_rules": len(rules),
        },
    )
    AccountActivityLog.objects.create(
        actor=actor, org=job.org, farm=job.farm,
        action="INSTALLER_JOB_BIND",
        details={
            "job_id": job.pk, "unit_id": unit.unit_id,
            "config_version": slot.desired_config_version,
            "via_override": via_override,
        },
    )
    return slot, slot_created


@extend_schema(
    tags=[TAG],
    operation_id="install_job_bind",
    summary="Bind a factory unit to this job by unit id + claim code (AD-3)",
    description="""The installer scans the QR label on the physical gateway and the
backend joins it to the job: creates the `gateway_slots` row (slot id = unit id,
AD-2), expands the ACL from the 3-row device lane to the full T1–T9 tenant set,
consumes the claim code, moves the job to BOUND, and enqueues the first
retained-configuration publish.

**Works with the unit OFFLINE** — nothing here talks to the broker, so a device
that is switched off can still be bound and will pick up its configuration on
its next connect.

The claim code is a possession proof, single use: two installers racing on the
same label cannot both win (the stamp happens inside the row lock). It is NOT
the MQTT password, so a photographed label cannot break authentication — only
`retire` can.

Idempotent for a REPLAYED bind of the same unit to the same job, because an
installer with one bar of signal presses Send twice.""",
    request=None,
    responses={200: None, 400: None, 403: None, 404: None, 409: None},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def install_job_bind(request, job_id: int):
    denied = staff_denied(request, QUEUE_ROLES)
    if denied is not None:
        return denied

    unit_id = str(request.data.get("unit_id") or "").strip()
    claim_code = str(request.data.get("claim_code") or "").strip()
    if not unit_id or not claim_code:
        return Response({"error": "unit_id and claim_code are required"}, status=400)

    job = _find_job(request, job_id)
    if job is None:
        return Response({"error": "job not found"}, status=404)
    if job.state in (InstallationJob.STATE_COMPLETED, InstallationJob.STATE_CANCELLED):
        return Response({"error": f"job is {job.state}"}, status=409)

    from apps.devices.models import DeviceUnit

    # Discovery over the admin alias first: the unit is ORG-LESS by design, so
    # there is no tenant context to read it in yet (the very problem AD-1
    # solves). Its org comes from the JOB, which we already discovered.
    unit = DeviceUnit.objects.get(pk=unit_id)

    if unit.state == "RETIRED":
        return Response({"error": "unit not found"}, status=404)

    # A replay of the SAME bind is a 200, not a 409 — see the docstring.
    if job.unit_id == unit_id and job.gateway_slot_id:
        return Response({"ok": True, "already_bound": True, "job": _job_out(job)})

    if job.gateway_slot_id and job.unit_id != unit_id:
        return Response(
            {"error": "job already bound to another unit", "bound_unit_id": job.unit_id},
            status=409,
        )
    if unit.state == "BOUND" and unit.slot_id != job.gateway_slot_id:
        return Response(
            {"error": "unit is already bound to another job", "bound": True},
            status=409,
        )
    if not unit.claim_code_hash:
        return Response({"error": "unit has no claim code"}, status=409)
    if unit.claimed_at is not None:
        return Response({"error": "claim code already consumed"}, status=409)
    if not verify_password(claim_code, unit.claim_code_hash):
        return Response({"error": "invalid claim code"}, status=400)

    org_id = str(job.org_id)
    with transaction.atomic(), tenant_context(org_id):
        _bind_job_to_unit(
            job, unit, actor=request.user, via_override=False, reason="",
            ip=request.META.get("REMOTE_ADDR"),
        )

    job = _find_job(request, job_id)
    return Response({"ok": True, "job": _job_out(job)})


@extend_schema(
    tags=[TAG],
    operation_id="install_job_bind_override",
    summary="Bind without a claim code (support/admin, lost label)",
    description="""Escape hatch for a label that was destroyed or never arrived.
Skips the claim-code proof, which is why `reason` is MANDATORY and the row is
audited in both ledger tables with `via_override: true`. The device's MQTT
credential is untouched, so an unproven unit can still be retired later.""",
    request=None,
    responses={200: None, 400: None, 403: None, 404: None, 409: None},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def install_job_bind_override(request, job_id: int):
    denied = staff_denied(request, ASSIGN_ROLES)
    if denied is not None:
        return denied

    unit_id = str(request.data.get("unit_id") or "").strip()
    reason = str(request.data.get("reason") or "").strip()
    if not unit_id:
        return Response({"error": "unit_id required"}, status=400)
    if not reason:
        return Response({"error": "reason is required for an override bind"}, status=400)

    job = _find_job(request, job_id)
    if job is None:
        return Response({"error": "job not found"}, status=404)
    if job.state in (InstallationJob.STATE_COMPLETED, InstallationJob.STATE_CANCELLED):
        return Response({"error": f"job is {job.state}"}, status=409)

    from apps.devices.models import DeviceUnit

    unit = DeviceUnit.objects.get(pk=unit_id)
    if unit.state == "RETIRED":
        return Response({"error": "unit not found"}, status=404)
    if job.unit_id == unit_id and job.gateway_slot_id:
        return Response({"ok": True, "already_bound": True, "job": _job_out(job)})
    if job.gateway_slot_id and job.unit_id != unit_id:
        return Response(
            {"error": "job already bound to another unit", "bound_unit_id": job.unit_id},
            status=409,
        )
    if unit.state == "BOUND" and unit.slot_id != job.gateway_slot_id:
        return Response(
            {"error": "unit is already bound to another job", "bound": True},
            status=409,
        )

    with transaction.atomic(), tenant_context(str(job.org_id)):
        _bind_job_to_unit(
            job, unit, actor=request.user, via_override=True, reason=reason,
            ip=request.META.get("REMOTE_ADDR"),
        )

    job = _find_job(request, job_id)
    return Response({"ok": True, "job": _job_out(job)})


# ---------------------------------------------------------------------------
# POST /provisioning/installations/{job_id}/submit/  — slice (d)
# ---------------------------------------------------------------------------

@extend_schema(
    tags=[TAG],
    operation_id="install_job_submit",
    summary="Submit a bound job (installer finished on site)",
    description="""The installer presses Done after the gateway is mounted and wired.
Moves the job BOUND → SUBMITTED, which is the condition AD-17 waits for: once
the device also reports that it applied the desired configuration, the slot
activates to `PROVISIONED` with NO human approval.

Requires a bound unit — you cannot submit a job with no device. Submitting
twice is idempotent, because the Installer presses Done again when the HTTP
response was lost.

If the device already reported before the installer reached for the phone, the
activation check runs immediately on submit rather than waiting for the next
report.""",
    request=None,
    responses={200: None, 403: None, 404: None, 409: None},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def install_job_submit(request, job_id: int):
    denied = staff_denied(request, QUEUE_ROLES)
    if denied is not None:
        return denied

    job = _find_job(request, job_id)
    if job is None:
        return Response({"error": "job not found"}, status=404)
    if job.state == InstallationJob.STATE_SUBMITTED:
        return Response({"ok": True, "already_submitted": True, "job": _job_out(job)})
    if job.state in (InstallationJob.STATE_COMPLETED, InstallationJob.STATE_CANCELLED):
        return Response({"error": f"job is {job.state}"}, status=409)
    if job.state != InstallationJob.STATE_BOUND:
        return Response(
            {"error": f"job is {job.state}; only a BOUND job can be submitted"},
            status=409,
        )

    org_id = str(job.org_id)
    with transaction.atomic(), tenant_context(org_id):
        job.state = InstallationJob.STATE_SUBMITTED
        job.save(update_fields=["state"])

        from apps.devices.models import GatewaySlot
        from apps.provisioning.tasks import _maybe_activate

        slot = GatewaySlot.objects.filter(pk=job.gateway_slot_id).first()
        if slot is not None:
            # The device may have reported APPLIED before the installer pressed
            # Done. Run the same check the report path runs, so the activation
            # does not wait for a report that will never come again.
            _maybe_activate(slot, InstallationJob)

    PlatformAuditLog.objects.create(
        actor=request.user, action="PLATFORM_JOB_SUBMIT", target_type="installation_job",
        target_id=str(job.pk), ip_address=request.META.get("REMOTE_ADDR"),
        details={"unit_id": job.unit_id, "slot_id": job.gateway_slot_id},
    )
    with transaction.atomic(), tenant_context(org_id):
        AccountActivityLog.objects.create(
            actor=request.user, org=job.org, farm=job.farm,
            action="INSTALLER_JOB_SUBMIT",
            details={"job_id": job.pk, "unit_id": job.unit_id},
        )

    job = _find_job(request, job_id)
    return Response({"ok": True, "job": _job_out(job)})


# ---------------------------------------------------------------------------
# GET /provisioning/config-state/  — slice (e), the four-state view (AD-22)
# ---------------------------------------------------------------------------

@extend_schema(
    tags=[TAG],
    operation_id="provisioning_config_state",
    summary="Four-state convergence view (support/admin)",
    description="""`?state=pending_sync|stalled|failed|ready` — the derived, never-stored
lifecycle from AD-22. This is the view a support engineer opens when a farm says
"my gateway shows nothing": PENDING_SYNC means the device was never told
anything, STALLED means it was told and has not caught up, FAILED carries the
reason, READY means it converged.

Every row is derived from theVersion counters at read time, so it cannot
disagree with the reconciler. Cross-org rows are invisible: an org-less
support caller reads through the admin alias and each org's rows are read
inside its own tenant context.""",
    parameters=[
        OpenApiParameter(
            "state", str, OpenApiParameter.QUERY, required=False,
            description="pending_sync | stalled | failed | ready",
        )
    ],
    responses={200: None, 400: None, 403: None},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provisioning_config_state(request):
    denied = staff_denied(request, ASSIGN_ROLES)
    if denied is not None:
        return denied

    wanted = str(request.query_params.get("state") or "").strip().upper()
    if wanted and wanted not in {"PENDING_SYNC", "STALLED", "FAILED", "READY"}:
        return Response({"error": "invalid state filter"}, status=400)

    from apps.devices.models import GatewaySlot

    rows = []

    def _collect(queryset):
        for slot in queryset.select_related("farm").order_by("-created_at")[:200]:
            view = _gateway_state_view(slot)
            if wanted and view != wanted:
                continue
            rows.append(
                {
                    "gateway_slot_id": slot.slot_id,
                    "org_id": str(slot.org_id),
                    "farm_id": slot.farm_id,
                    "farm_name": slot.farm.name if slot.farm_id else None,
                    "provisioning_status": slot.provisioning_status,
                    "state": view,
                    "desired_config_version": slot.desired_config_version,
                    "applied_config_version": slot.applied_config_version,
                    "last_publish_error": slot.last_publish_error,
                    "last_reported_at": (
                        slot.last_reported_at.isoformat() if slot.last_reported_at else None
                    ),
                    "applied_at": slot.applied_at.isoformat() if slot.applied_at else None,
                }
            )

    if _is_orgless_staff(request):
        from apps.tenants.models import Organization

        for org_id in Organization.objects.using(admin_alias()).values_list(
            "org_id", flat=True
        ):
            with transaction.atomic(), tenant_context(str(org_id)):
                try:
                    _collect(GatewaySlot.objects.filter(org_id=org_id))
                except Exception:  # noqa: BLE001 — one unreadable org must not hide the rest
                    logger.exception("config-state discovery failed for org=%s", org_id)
    else:
        org_id = _claims(request).get("org_id")
        _collect(GatewaySlot.objects.filter(org_id=org_id) if org_id else GatewaySlot.objects.none())

    return Response({"count": len(rows), "results": rows})


# ---------------------------------------------------------------------------
# POST /provisioning/installations/{job_id}/sensors/  — slice f, task 2.10
# ---------------------------------------------------------------------------

def _pack_id_ok(pack_id: str) -> bool:
    """AD-23: `pack_id` is the 8-hex LoRa node id — the join key, not metadata.

    Strict on purpose. A typo'd node id here becomes a sensor the device never
    reports for, and the failure surfaces as a "stalled" gateway on a farm that
    looks installed — the most expensive class of bug to trace back.
    """
    return (
        len(pack_id) == 8
        and all(c in "0123456789abcdefABCDEF" for c in pack_id)
    )


@extend_schema(
    tags=[TAG],
    operation_id="install_job_sensor_map",
    summary="Bind a sensor pack to a sensor slot (AD-23)",
    description="""The join that makes a sensor slot real: `pack_id` is the 8-hex LoRa
node id burned into the sensor at manufacture and printed on it, and this is
where it meets the `saloon_sensor_slots` row the installer created.

Everything the device needs arrives through this join — `sensors[].pack_id`,
its saloon, position, label and `metrics` (from the slot's
`allowed_metric_keys`, the column that previously had no writer). So the mapping
bumps `desired_config_version`, which is what makes the device treat the next
retained document as NEW rather than a stale copy.

One physical sensor can only be in one place: mapping the same `pack_id` to a
DIFFERENT slot is a 409. Mapping it to the SAME slot again is idempotent (200),
because an installer with one bar of signal presses Send twice.""",
    request=None,
    responses={200: None, 201: None, 400: None, 403: None, 404: None, 409: None},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def install_job_sensor_map(request, job_id: int):
    denied = staff_denied(request, QUEUE_ROLES)
    if denied is not None:
        return denied

    pack_id = str(request.data.get("pack_id") or "").strip()
    slot_id = str(request.data.get("slot_id") or "").strip()
    if not pack_id or not slot_id:
        return Response({"error": "pack_id and slot_id are required"}, status=400)
    if not _pack_id_ok(pack_id):
        return Response(
            {"error": "pack_id must be the 8-hex LoRa node id (e.g. a1b2c3d4)"},
            status=400,
        )

    job = _find_job(request, job_id)
    if job is None:
        return Response({"error": "job not found"}, status=404)
    if job.gateway_slot_id is None:
        return Response({"error": "job has no gateway bound yet"}, status=409)
    if job.state in (InstallationJob.STATE_COMPLETED, InstallationJob.STATE_CANCELLED):
        return Response({"error": f"job is {job.state}"}, status=409)

    from apps.devices.models import (
        GatewaySensorMapping,
        GatewaySlot,
        SaloonSensorSlot,
    )
    from apps.provisioning.desired_state import gateway_hardware_id

    org_id = str(job.org_id)
    with transaction.atomic(), tenant_context(org_id):
        # The slot must be a mounting position in THIS job's farm, not any slot
        # in the org — otherwise an installer could map a pack onto a saloon in
        # a different building.
        sensor_slot = SaloonSensorSlot.objects.filter(
            pk=slot_id, org_id=org_id, saloon__farm_id=job.farm_id
        ).first()
        if sensor_slot is None:
            return Response(
                {"error": "sensor slot not found in this job's farm"}, status=404
            )

        gateway_slot = GatewaySlot.objects.filter(pk=job.gateway_slot_id).first()
        if gateway_slot is None:
            return Response({"error": "gateway slot missing"}, status=404)
        hw_id = gateway_hardware_id(gateway_slot)
        if not hw_id:
            return Response(
                {"error": "gateway has no hardware identity to map against"}, status=409
            )

        existing = GatewaySensorMapping.objects.filter(pack_id=pack_id).first()
        if existing is not None:
            if existing.slot_id == slot_id and existing.gateway_hardware_id == hw_id:
                return Response(
                    {"ok": True, "already_mapped": True,
                     "pack_id": pack_id, "slot_id": slot_id}
                )
            return Response(
                {"error": "pack is already mapped to another slot",
                 "mapped_slot_id": existing.slot_id,
                 "mapped_gateway": existing.gateway_hardware_id},
                status=409,
            )

        GatewaySensorMapping.objects.create(
            org_id=org_id,
            gateway_hardware_id=hw_id,
            pack_id=pack_id,
            slot=sensor_slot,
        )

        # AD-23/AD-4: bump so the device sees a NEW document carrying this
        # sensor. Without the bump a device that already applied config_version 1
        # would refuse the identical retained copy.
        gateway_slot.desired_config_version += 1
        gateway_slot.save(update_fields=["desired_config_version"])

        # Publish on commit, not after — an installer standing in a saloon is
        # waiting for this sensor to reach the gateway. Leaving it to the 60s
        # convergence sweep would mean a minute of "the device never got it"
        # on a farm that looks installed. on_commit keeps the ordering right: a
        # device must never be told about state we have not committed yet.
        def _publish():
            from apps.provisioning.tasks import publish_desired_state_for

            try:
                publish_desired_state_for(gateway_slot)
            except Exception:  # noqa: BLE001 — the sweep is the safety net
                logger.exception(
                    "desired-state publish failed after mapping a sensor on %s",
                    gateway_slot.slot_id,
                )

        transaction.on_commit(_publish)

    PlatformAuditLog.objects.create(
        actor=request.user, action="PLATFORM_JOB_SENSOR_MAP",
        target_type="installation_job", target_id=str(job.pk),
        ip_address=request.META.get("REMOTE_ADDR"),
        details={"pack_id": pack_id, "slot_id": slot_id, "hardware_id": hw_id,
                 "config_version": gateway_slot.desired_config_version},
    )
    with transaction.atomic(), tenant_context(org_id):
        AccountActivityLog.objects.create(
            actor=request.user, org=job.org, farm=job.farm,
            action="INSTALLER_JOB_SENSOR_MAP",
            details={"job_id": job.pk, "pack_id": pack_id, "slot_id": slot_id},
        )

    return Response(
        {"ok": True, "pack_id": pack_id, "slot_id": slot_id,
         "config_version": gateway_slot.desired_config_version},
        status=201,
    )


# ---------------------------------------------------------------------------
# Photo uploads (AD-20, task 2.9) — device_photos
# ---------------------------------------------------------------------------

# Photo rules are NOT re-invented here. `ops_services.photo_error` /
# `ALLOWED_PHOTO_TYPES` already implement the spec §2 O6 limits (<=10 MB,
# jpeg/png) and the saloon-photo upload path uses them, so the device-photo
# endpoint inherits the identical contract instead of drifting from it.
from apps.monitoring import ops_services as photo_svc


def _store_device_photo(request, *, org_id, actor, gateway_slot_id=None, sensor_slot_id=None,
                        description=None, is_primary=False):
    """Validate, store and record one DevicePhoto (AD-20).

    AD-20's rule is that the target is EXACTLY ONE of gateway/sensor — enforced
    by the model's CHECK constraint and asserted here before the write, so a bad
    request is a 400 rather than a 500 from the database.

    Storage is the existing local backend (`MEDIA_ROOT`, settings/base.py) with a
    deterministic path, because production already serves `/media/` from the
    mounted volume. `file_url` is what the client sees, never the absolute path.
    """
    import uuid as _uuid

    from django.conf import settings
    from django.core.files.storage import default_storage

    from apps.devices.models import DevicePhoto

    if bool(gateway_slot_id) == bool(sensor_slot_id):
        return Response(
            {"error": "exactly one of gateway_slot_id / sensor_slot_id"}, status=400
        )

    upload = request.FILES.get("file")
    if upload is None:
        return Response({"error": "file is required (multipart)"}, status=400)
    # Same validator, same limits, same error text as the saloon-photo path.
    error = photo_svc.photo_error(upload)
    if error:
        return Response({"error": error}, status=400)

    target = gateway_slot_id or sensor_slot_id
    where = "gateway" if gateway_slot_id else "sensor"
    extension = photo_svc.ALLOWED_PHOTO_TYPES[upload.content_type]
    relative = (
        f"device-photos/{org_id}/{where}/{target}/"
        f"{_uuid.uuid4().hex}{extension}"
    )
    saved = default_storage.save(relative, upload)

    with transaction.atomic(), tenant_context(org_id):
        # A single primary per target — the gallery's cover image. Clearing the
        # others first keeps the invariant total instead of "the last one wins".
        if is_primary:
            DevicePhoto.objects.filter(
                org_id=org_id,
                gateway_slot_id=gateway_slot_id,
                sensor_slot_id=sensor_slot_id,
                is_primary=True,
            ).update(is_primary=False)

        photo = DevicePhoto.objects.create(
            org_id=org_id,
            gateway_slot_id=gateway_slot_id,
            sensor_slot_id=sensor_slot_id,
            uploaded_by=actor,
            file_url=settings.MEDIA_URL + saved,
            description=description,
            is_primary=bool(is_primary),
        )

    PlatformAuditLog.objects.create(
        actor=actor, action="PLATFORM_DEVICE_PHOTO_UPLOAD",
        target_type="device_photo", target_id=str(photo.pk),
        ip_address=request.META.get("REMOTE_ADDR"),
        details={"where": where, "target": target, "bytes": upload.size,
                 "content_type": upload.content_type},
    )
    with transaction.atomic(), tenant_context(org_id):
        AccountActivityLog.objects.create(
            actor=actor, org_id=org_id,
            action="DEVICE_PHOTO_UPLOAD",
            details={"where": where, "target": target, "photo_id": photo.pk},
        )

    return Response(
        {
            "ok": True,
            "photo_id": photo.pk,
            "file_url": photo.file_url,
            "where": where,
            "target": target,
            "is_primary": photo.is_primary,
        },
        status=201,
    )


@extend_schema(
    tags=[TAG],
    operation_id="install_sensor_photo",
    summary="Upload an installation photo of a sensor (AD-20)",
    description="""AD-20: "each sensor is an independent entity in our system", so
each one gets its own photo — the installer's proof of what was actually mounted
where. Accepts a multipart `file` (jpeg/png/webp, max 5 MB) plus an optional
`description` and `is_primary`.

`is_primary` clears the previous primary for the SAME slot, so a target has one
cover image rather than a race. The sensor slot must belong to a saloon in this
job's farm.""",
    request=None,
    responses={200: None, 201: None, 400: None, 403: None, 404: None},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def install_sensor_photo(request, job_id: int, slot_id: str):
    denied = staff_denied(request, QUEUE_ROLES)
    if denied is not None:
        return denied

    job = _find_job(request, job_id)
    if job is None:
        return Response({"error": "job not found"}, status=404)

    from apps.devices.models import SaloonSensorSlot

    with transaction.atomic(), tenant_context(str(job.org_id)):
        target = SaloonSensorSlot.objects.filter(
            pk=slot_id, org_id=job.org_id, saloon__farm_id=job.farm_id
        ).first()
    if target is None:
        return Response({"error": "sensor slot not found in this job's farm"}, status=404)

    return _store_device_photo(
        request,
        org_id=str(job.org_id),
        actor=request.user,
        sensor_slot_id=target.slot_id,
        description=request.data.get("description"),
        is_primary=str(request.data.get("is_primary", "")).lower() in ("1", "true", "yes"),
    )


@extend_schema(
    tags=[TAG],
    operation_id="install_gateway_photo",
    summary="Upload an installation photo of the gateway (AD-20)",
    description="""AD-20's other half: a photo of the gateway itself, so a later
technician can see how it was mounted and wired. Same rules as the sensor photo —
multipart `file`, jpeg/png/webp, 5 MB, one primary.""",
    request=None,
    responses={200: None, 201: None, 400: None, 403: None, 404: None, 409: None},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def install_gateway_photo(request, job_id: int):
    denied = staff_denied(request, QUEUE_ROLES)
    if denied is not None:
        return denied

    job = _find_job(request, job_id)
    if job is None:
        return Response({"error": "job not found"}, status=404)
    if not job.gateway_slot_id:
        return Response({"error": "no gateway bound to this job"}, status=409)

    return _store_device_photo(
        request,
        org_id=str(job.org_id),
        actor=request.user,
        gateway_slot_id=job.gateway_slot_id,
        description=request.data.get("description"),
        is_primary=str(request.data.get("is_primary", "")).lower() in ("1", "true", "yes"),
    )
