"""Auth endpoints — /auth/login/ and /auth/token/refresh/ (contract v1.4.3).

WHY: the OpenAPI contract is CANON, so the login RESPONSE shape is adapted in
code: {access_token, refresh_token, user{UserProfile}} — NOT SimpleJWT's stock
{access, refresh}. The refresh endpoint stays STOCK because RefreshResponse
deliberately uses native `{access}` only; that asymmetry is contractual
(TUTORIAL gotcha table) and must not be "unified".
The user envelope reuses resolve_active_membership() so claims and profile can
never disagree about who logged in (one rule, two consumers).

فارسی: شکلِ پاسخِ لاگین از قراردادِ API می‌آید، نه از پیش‌فرض کتابخانه؛ توکنِ
نوسازی عمداً به‌صورت استاندارد و با کلید access برمی‌گردد — ناهمگونی کلیدها
قراردادی است و نباید یکسان‌سازی شود.
"""

import pyotp
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts import mfa
from apps.accounts.models import OrgMember
from apps.accounts.services import (
    register_session,
    refresh_is_revoked,
    touch_session,
)
from apps.accounts.permissions import _claims
from apps.accounts.serializers import LoginSerializer, resolve_active_membership
from apps.auditing.models import AccountActivityLog
from apps.tenants.context import tenant_context
from apps.tenants.models import Organization
from apps.tenants.models import Organization

# Contract v1.4.3 OrgMemberInviteRequest.role enum — the ONLY writable roles.
ORG_MEMBER_ROLES = ["owner", "manager", "engineer", "doctor", "worker", "installer"]

User = get_user_model()


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # M7-07 (Security Architecture §4.5 activation): platform-staff logins
        # MUST carry a live TOTP code against a CONFIRMED enrollment — the
        # support surface is the crown jewels. superuser is deliberately NOT
        # gated (break-glass System Admin; the support surface itself rejects
        # superusers). Tenant users are never asked for a code (Phase 2).
        mfa_code = request.data.get("mfa_code")
        if (
            serializer.user.is_platform_staff
            and not serializer.user.is_superuser
            and not mfa.verify_login_code(serializer.user, mfa_code)
        ):
            enrolled = mfa.is_confirmed(serializer.user)
            return Response(
                {
                    "error": "mfa_required",
                    "detail": (
                        "platform-staff accounts must verify a TOTP code"
                        if enrolled
                        else "no confirmed MFA enrollment — POST /auth/mfa/setup/"
                    ),
                    "enrolled": enrolled,
                },
                status=403,
            )
        # M6-10 (O11/O12): one user_sessions row per refresh token. SimpleJWT
        # hands validated_data["refresh"] over as a STRING — parse it to read
        # the jti the register keys on. Best-effort: a register failure must
        # never block a valid login (services.py wraps its own errors too).
        register_session(serializer.user, RefreshToken(serializer.validated_data["refresh"]), request)
        member, roles = resolve_active_membership(serializer.user)
        # Platform staff log in ORG-LESS (D15): no membership, no org envelope.
        return Response(
            {
                "access_token": serializer.validated_data["access"],
                "refresh_token": serializer.validated_data["refresh"],
                "user": {
                    "id": serializer.user.id,
                    "username": serializer.user.username,
                    "roles": roles,
                    "org_id": str(member.org_id) if member else None,
                    "org_name": member.org.name if member else None,
                    "is_platform_staff": bool(serializer.user.is_platform_staff),
                },
            }
        )


class RefreshView(TokenRefreshView):
    """Stock endpoint, one override: tenancy claims are RE-DERIVED at refresh.

    Contract v1.4.3 promises a deactivated member's next refresh yields an
    access WITHOUT org_id — but stock refresh COPIES the frozen claims from
    the old refresh token, which would keep a fired worker reading their old
    farm for up to 7 days. So the new access is minted fresh from CURRENT DB
    truth (resolve_active_membership): active membership ⇒ same claims as
    login; deactivated/staff ⇒ org-less (branch-②/④ semantics, C-14).
    فارسی: در نوسازی، ادعاهای سازمانی از حقیقتِ فعلیِ دیتابیس ساخته می‌شوند تا
    خروجِ عضو بلافاصله اثر کند و توکنِ جدید بدونِ org_id صادر شود.
    """

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code != 200:
            return response
        try:
            old = RefreshToken(request.data.get("refresh"))
        except TokenError:
            return response  # unreachable after super() succeeded; stay safe
        user = User.objects.filter(id=old.payload.get("user_id")).first()
        if user is None:
            return response
        # M6-10 (O12): a revoked session's refresh line dies HERE — the
        # register (not the never-installed blacklist app) is the authority.
        # Pre-M6-10 tokens carry no sid/row: they age out within REFRESH
        # TOKEN_LIFETIME (7 d) and are simply outside the register.
        if refresh_is_revoked(old):
            return Response({"detail": "session revoked"}, status=401)
        # sid = the refresh token's jti: the refreshed access carries it so
        # O11 marks exactly this line `current` and O12 revokes exactly it.
        old["sid"] = old.payload.get("jti")
        touch_session(old)  # last_seen_at bump (never un-revokes: filtered)
        try:
            member, roles = resolve_active_membership(user)
        except AuthenticationFailed:
            # Fully deactivated tenant: the refresh itself STAYS valid (its own
            # lifetime is the accepted risk, Security §3.2) but the new access
            # must be ORG-LESS — C-14's fail-closed-empty semantics.
            member, roles = None, []
        access = AccessToken.for_user(user)
        access["roles"] = roles
        if member is not None:
            access["org_id"] = str(member.org_id)
        if user.is_platform_staff:
            access["is_platform_staff"] = True
        if old.payload.get("jti"):
            access["sid"] = old.payload.get("jti")
        response.data["access"] = str(access)
        return response


def _member_out(member: OrgMember) -> dict:
    """Contract v1.4.3 OrgMemberOut shape."""
    joined = member.joined_at.isoformat() if member.joined_at else None
    return {
        "member_id": member.id,
        "user_id": member.user_id,
        "username": member.user.username,
        "role": member.role,
        "is_active": bool(member.is_active),
        "joined_at": joined,
    }


def _require_owner(request):
    """403 response unless the validated token carries the owner role."""
    if "owner" not in set(_claims(request).get("roles", [])):
        return Response({"error": "caller is not the org owner"}, status=403)
    return None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def org_members(request):
    """/org/members/ — list (any member) and invite (owner-exclusive).

    The org comes from the VALIDATED org_id claim, mirroring what the
    middleware GUC enforces at the DB layer on Postgres; filtering here too is
    deliberate defense-in-depth so sqlite loops behave identically.

    فارسی: فهرست اعضا برای همهٔ اعضا؛ دعوت فقط با نقش مالک. نام کاربریِ تازه با
    رمز غیرقابل‌استفاده ساخته می‌شود؛ دعوت روی عضویت غیرفعال آن را فعال می‌کند
    (200) و عضویت فعالِ تکراری ۴۰۹ می‌دهد.
    """
    claims = _claims(request)
    org_id = claims.get("org_id")

    if request.method == "GET":
        members = OrgMember.objects.filter(org_id=org_id).order_by("id")
        return Response([_member_out(m) for m in members])

    forbidden = _require_owner(request)
    if forbidden is not None:
        return forbidden

    username = (request.data.get("username") or "").strip()
    role = request.data.get("role")
    if not username or role not in ORG_MEMBER_ROLES:
        return Response({"error": "username and a valid role are required"}, status=400)

    user = User.objects.filter(username=username).first()
    if user is None:
        # Contract: created with an UNUSABLE password — login happens only via
        # an explicit credential handover, never by our invite.
        user = User(username=username)
        user.set_unusable_password()
        user.save()

    member = OrgMember.objects.filter(user=user, org_id=org_id).first()
    if member is not None and member.is_active:
        return Response({"error": "an active membership already exists"}, status=409)
    if member is not None:
        member.is_active = True  # reactivate — UNIQUE(org,user) stands, row reused
        member.save(update_fields=["is_active"])
        return Response(_member_out(member), status=200)

    member = OrgMember.objects.create(user=user, org_id=org_id, role=role, is_active=True)
    return Response(_member_out(member), status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def deactivate_member(request, member_id: int):
    """/org/members/{id}/deactivate/ — soft-off, never delete (contract v1.4.3).

    Consequence chain (Security §4.1b, C-14): live tokens die ≤15 min; next
    refresh yields an org-less token ⇒ fail-closed-empty HTTP 200 reads.

    فارسی: غیرفعال‌سازی نرم — ردیف هرگز حذف نمی‌شود؛ توکن‌های زنده تا ۱۵ دقیقه
    می‌میرند و نوسازی بعدی توکنِ بدون سازمان می‌دهد.
    """
    forbidden = _require_owner(request)
    if forbidden is not None:
        return forbidden

    org_id = _claims(request).get("org_id")
    member = OrgMember.objects.filter(id=member_id, org_id=org_id).first()
    if member is None:
        return Response({"error": "no such member in the caller's org"}, status=404)
    member.is_active = False
    member.save(update_fields=["is_active"])
    return Response({"is_active": False})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def support_session(request):
    """/platform/support/session/ — audited org-context session (Security §4.5).

    Four policies, in order (TUTORIAL §3.5 verbatim semantics):
      1. staff-NOT-superuser only — superuser is deliberately rejected (403);
      2. incident_id is MANDATORY and non-blank (400) — the audit anchor;
      3. the scoped token lives 15 min (expires_in_sec 900), claims add
         org_id + support_session:true + incident_id; middleware branch ①
         makes its holder an ordinary RLS-scoped user of exactly that org;
      4. SUPPORT_ORG_ENTER audit row is written BEFORE the success return.

    فارسی: نشست پشتیبانی فقط برای کارکنانِ غیرسوپریوزر؛ شماره حادثه اجباری؛ توکن
    ۱۵ دقیقه‌ای با ادعای سازمانِ مقصد؛ و ردیف ممیز پیش از بازگشت موفق ثبت می‌شود.
    """
    user = request.user
    if not user.is_platform_staff or user.is_superuser:
        return Response({"error": "caller lacks the support platform claim"}, status=403)

    incident = (request.data.get("incident_id") or "").strip()
    if not incident:
        return Response({"error": "incident_id required"}, status=400)

    raw_org_id = (request.data.get("org_id") or "").strip()
    try:
        org_uuid = str(UUID(raw_org_id))
    except (ValueError, AttributeError):
        return Response({"error": "unknown org_id"}, status=400)

    # M7-10 live-drill fix: staff tokens are ORG-LESS by design (D15), so the
    # middleware sets no GUC for this caller — and organizations is FORCE-RLS'd,
    # so the lookup below saw ZERO rows for EVERY org on prod (the M7-07 suite
    # runs as the DB superuser, which BYPASSRLS's straight past this — the same
    # masking class the M7-06 db_fetch fix already paid for). Scope the lookup
    # AND the audit write to the REQUESTED org via tenant_context: a wrong UUID
    # still 400s, no privilege widens — the opener only ever returns the one
    # org the caller named.
    #
    # فارسی: نشست پشتیبانی برای کارکنانِ بدون سازمان است، پس خواندن سازمان و
    # ثبت ممیزی باید در زمینهٔ همان سازمانِ درخواستی انجام شود؛ وگرنه RLS
    # همه‌چیز را پنهان می‌کند و نشست هرگز باز نمی‌شود.
    with transaction.atomic(), tenant_context(org_uuid):
        org = Organization.objects.filter(org_id=org_uuid).first()
        if org is None:
            return Response({"error": "unknown org_id"}, status=400)

        token = AccessToken.for_user(user)
        token["org_id"] = str(org.org_id)
        token["support_session"] = True
        token["incident_id"] = incident

        # Audit BEFORE success — the write must not be skippable by a later error.
        AccountActivityLog.objects.create(
            actor=user,
            org=org,
            action="SUPPORT_ORG_ENTER",
            ip_address=request.META.get("REMOTE_ADDR"),
            details={"incident_id": incident, "reason": request.data.get("reason", "")},
        )

        return Response(
            {
                "access_token": str(token),
                "org_id": str(org.org_id),
                "incident_id": incident,
                "expires_in_sec": 900,
            }
        )


# ---------------------------------------------------------------------------
# M7-07 — platform-staff MFA enrollment (Security Architecture §4.5).
# Two-step: setup (issue secret) → confirm (first live code flips the row).
# Staff-only + superuser-rejected, mirroring the support surface itself.
# ---------------------------------------------------------------------------
def _staff_not_superuser(request):
    user = request.user
    if not user.is_platform_staff or user.is_superuser:
        return Response({"error": "caller lacks the support platform claim"}, status=403)
    return None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mfa_setup(request):
    """POST /auth/mfa/setup/ — (re)issue an unconfirmed TOTP enrollment.

    Returns the secret (hex) and the otpauth:// URI the authenticator app
    consumes. Until /auth/mfa/confirm/ verifies a live code, staff login
    stays blocked (enroll-then-login is the intended first-run flow).
    """
    denied = _staff_not_superuser(request)
    if denied is not None:
        return denied
    row = mfa.provision(request.user)
    return Response(
        {
            "secret": row.secret,
            "otpauth_uri": mfa.provisioning_uri(request.user, row.secret),
            "confirmed": False,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mfa_confirm(request):
    """POST /auth/mfa/confirm/ — verify the first live code; flips
    confirmed_at so login opens. A wrong code is a plain 400 (retryable)."""
    denied = _staff_not_superuser(request)
    if denied is not None:
        return denied
    code = request.data.get("code")
    if not code:
        return Response({"error": "code required"}, status=400)
    if not mfa.confirm(request.user, code):
        return Response({"error": "invalid or already-confirmed code"}, status=400)
    return Response({"confirmed": True})


# ---------------------------------------------------------------------------
# M7-07 — the 3 whitelisted support ops (v1.2 GS-SUPPORT; Security §4.5).
# Shared gate: an ACTIVE support-session token (middleware branch ①) whose
# org_id scopes the op; every success writes a SUPPORT_OP_* audit row with
# the session's incident_id. superuser is NEVER a valid caller (403).
# ---------------------------------------------------------------------------
def _support_op_context(request):
    """(org_id, incident_id) from claims, or a Response rejecting the call.

    TWO gates, both mandatory (Security §4.5):
    - the CALLER is staff-NOT-superuser (superuser is never a support actor —
      a forged support_session claim on a superuser token must not open the
      surface; the session opener already rejects superusers, this is the
      second lock on the same door);
    - the TOKEN carries an ACTIVE session (support_session + org_id claims —
      a staff token without one has no org scope; a tenant token fails the
      same gate: no existence oracle, one deny for everyone).
    """
    user = request.user
    if not user.is_platform_staff or user.is_superuser:
        return None, None, Response(
            {"error": "no active support session for this org"}, status=403
        )
    claims = _claims(request)
    if not claims.get("support_session") or not claims.get("org_id"):
        return None, None, Response(
            {"error": "no active support session for this org"}, status=403
        )
    return claims.get("org_id"), claims.get("incident_id"), None


def _audit_support_op(request, org_id, action, incident_id, details):
    """SUPPORT_OP_* row — the tenant-facing accountability delta of every op."""
    AccountActivityLog.objects.create(
        actor=request.user,
        org_id=org_id,
        action=action,
        ip_address=request.META.get("REMOTE_ADDR"),
        details={"incident_id": incident_id, **details},
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def support_gateway_provisioning(request, gateway_slot_id: str):
    """POST /platform/support/gateways/{id}/provisioning/ — suspend/resume.

    action=suspend flips PROVISIONED → SUSPENDED and revokes the gateway's
    EMQX ACLs (traffic stops); resume restores the prior status and re-syncs
    the allows. Reuses the FM-side service functions wholesale — same state
    matrix, same ACL behavior, same PROVISIONING_STATUS_CHANGED trail — with
    the SUPPORT_OP_* row added on top (the accountability delta).
    """
    org_id, incident_id, denied = _support_op_context(request)
    if denied is not None:
        return denied
    action = request.data.get("action")
    if action not in {"suspend", "resume"}:
        return Response({"error": "action must be suspend or resume"}, status=400)

    from apps.devices.models import GatewaySlot

    slot = GatewaySlot.objects.filter(pk=gateway_slot_id, org_id=org_id).first()
    if slot is None:
        return Response({"error": "gateway not found in the session org"}, status=404)

    from apps.provisioning.services import resume_gateway, suspend_gateway

    try:
        if action == "suspend":
            result = suspend_gateway(
                slot.slot_id, request.user,
                reason=str(request.data.get("reason") or "support suspension"),
            )
        else:
            result = resume_gateway(slot.slot_id, request.user)
    except ValueError as exc:  # state-matrix violation → 409, no write
        return Response({"error": str(exc)}, status=409)

    _audit_support_op(
        request, org_id, "SUPPORT_OP_GATEWAY_PROVISIONING", incident_id,
        {"gateway_slot_id": slot.slot_id, "action": action,
         "provisioning_status": result.provisioning_status,
         "reason": request.data.get("reason", "")},
    )
    return Response(
        {"gateway_slot_id": slot.slot_id, "provisioning_status": result.provisioning_status}
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def support_alert_force_resolve(request, alert_id):
    """POST /platform/support/alerts/{id}/force-resolve/ — runbook-only op for
    alerts stuck by a known engine defect. Normal resolution stays hysteresis-
    driven; this writes resolved_at DIRECTLY (idempotent on already-resolved).
    """
    org_id, incident_id, denied = _support_op_context(request)
    if denied is not None:
        return denied
    reason = str(request.data.get("reason") or "").strip()
    if not reason:
        return Response({"error": "reason required (the runbook defect reference)"}, status=400)

    from apps.alerts.models import AlertInstance

    alert = AlertInstance.objects.filter(pk=alert_id, org_id=org_id).first()
    if alert is None:
        return Response({"error": "alert not found in the session org"}, status=404)
    if alert.resolved_at is not None:
        return Response({"alert_id": str(alert.pk), "resolved": True, "idempotent": True})

    alert.resolved_at = timezone.now()
    alert.current_status = "RESOLVED"
    alert.save(update_fields=["resolved_at", "current_status"])

    _audit_support_op(
        request, org_id, "SUPPORT_OP_ALERT_FORCE_RESOLVE", incident_id,
        {"alert_id": str(alert.pk), "reason": reason},
    )
    return Response({"alert_id": str(alert.pk), "resolved": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def support_task_dispatch(request):
    """POST /platform/support/tasks/drain/ — trigger a whitelisted maintenance
    task (drain_pending_approval / cache rebuilds). 202 = dispatched; the task
    body runs on the worker with the session org's scope passed explicitly.
    """
    org_id, incident_id, denied = _support_op_context(request)
    if denied is not None:
        return denied
    task = request.data.get("task")
    reason = str(request.data.get("reason") or "").strip()
    if task not in {"drain_pending_approval", "rebuild_threshold_cache", "rebuild_zone_cache"}:
        return Response({"error": "unknown task"}, status=400)
    if not reason:
        return Response({"error": "reason required"}, status=400)

    dispatched = ""
    if task == "drain_pending_approval":
        gateway_slot_id = request.data.get("gateway_slot_id")
        if not gateway_slot_id:
            return Response({"error": "task requires gateway_slot_id"}, status=400)
        from apps.devices.models import GatewaySlot

        slot = GatewaySlot.objects.filter(pk=gateway_slot_id, org_id=org_id).first()
        if slot is None:
            return Response({"error": "gateway not found in the session org"}, status=404)
        # Dispatch to the worker (the queue the drained entries' INSERT path
        # lives on) — the worker carries its own Redis/DB topology and the
        # approval drain is exactly its M5-04 contract.
        from apps.telemetry.tasks import drain_pending_for_gateway

        drain_pending_for_gateway.delay(slot.slot_id)
        dispatched = f"drain dispatched for {slot.slot_id}"
    elif task == "rebuild_threshold_cache":
        from apps.alerts.tasks import cache_daily_thresholds

        cache_daily_thresholds.delay()
        dispatched = "threshold recache dispatched"
    else:  # rebuild_zone_cache — the provisioning location tree (cache_key location:tree:{org})
        from django.core.cache import cache

        cache.delete(f"location:tree:{org_id}")
        dispatched = "location tree cache invalidated"

    _audit_support_op(
        request, org_id, "SUPPORT_OP_TASK_DISPATCH", incident_id,
        {"task": task, "result": dispatched, "reason": reason},
    )
    return Response({"task": task, "dispatched": dispatched}, status=202)
