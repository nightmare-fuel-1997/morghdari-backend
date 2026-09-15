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
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts import mfa
from apps.accounts.models import OrgMember, UserSession
from apps.accounts.services import (
    register_session,
    refresh_is_revoked,
    touch_session,
)
from apps.accounts.permissions import _claims
from apps.accounts.serializers import (
    LOGIN_REQUEST_EXAMPLE,
    LOGIN_RESPONSE_EXAMPLE,
    LoginRequestSerializer,
    LoginResponseSerializer,
    LoginSerializer,
    MEMBER_INVITE_EXAMPLE,
    MEMBER_OUT_EXAMPLE,
    MFA_SETUP_RESPONSE_EXAMPLE,
    MfaRequiredSerializer,
    SUPPORT_SESSION_REQUEST_EXAMPLE,
    SUPPORT_SESSION_RESPONSE_EXAMPLE,
    MemberDeactivateResponseSerializer,
    MemberRoleUpdateRequestSerializer,
    MemberInviteRequestSerializer,
    MemberOutSerializer,
    MfaConfirmRequestSerializer,
    MfaConfirmResponseSerializer,
    MfaSetupResponseSerializer,
    RefreshRequestSerializer,
    RefreshResponseSerializer,
    SupportForceResolveRequestSerializer,
    SupportForceResolveResponseSerializer,
    SupportGatewayProvisioningRequestSerializer,
    SupportGatewayProvisioningResponseSerializer,
    SupportSessionRequestSerializer,
    SupportSessionResponseSerializer,
    SupportTaskDispatchRequestSerializer,
    SupportTaskDispatchResponseSerializer,
    resolve_active_membership,
)
from apps.common.schemas import (
    CONFLICT_RESPONSE,
    TAG_AUTH,
    TAG_ORG,
    TAG_SUPPORT,
    ErrorEnvelopeSerializer,
    error_responses,
)
from apps.auditing.models import AccountActivityLog
from apps.monitoring.ops_throttles import LoginRateThrottle
from apps.tenants.context import tenant_context
from apps.tenants.models import Organization
from apps.tenants.models import Organization

# Contract v1.4.3 OrgMemberInviteRequest.role enum — the ONLY writable roles.
# v1.7.2: `reviewer` added. The v1.5 reports wave DEFINED that role (read-only
# on daily reports, by absence from every write role set) but never made it
# grantable, so an org could not actually produce one: the invite answered 400
# and the membership row could only be edited from the ORM. Both the invite and
# the new role-change endpoint accept it now.
ORG_MEMBER_ROLES = ["owner", "manager", "engineer", "doctor", "worker", "installer", "reviewer"]

User = get_user_model()


class LoginView(TokenObtainPairView):
    """POST /api/v1/auth/login/ — brute-force gated (BUG-5, QA 1.26).

    BUG-5 (QA 1.26): auth_login 60/min per attempted username — generous on
    purpose so the suite's legit login bursts (densest module 43 total, 13 on
    one username) never 429; a single-account bombing run still does.
    فارسی: نرخ‌گیرِ ورود ۶۰ در دقیقه برای هر نامِ کاربری تا حمله مهار شود و
    ورودهای سالمِ آزمون‌ها بسته نشوند.
    """

    serializer_class = LoginSerializer
    throttle_classes = [LoginRateThrottle]

    @extend_schema(
        tags=[TAG_AUTH],
        operation_id="login",
        summary="Log in (tenant or platform staff)",
        description="""Exchanges credentials for the contract's token pair —
`{access_token, refresh_token, user{…}}`, **not** SimpleJWT's stock
`{access, refresh}` keys; the response shape is canon (Security §4.1b).

Claims frozen at token birth: `org_id` = the caller's OLDEST active membership,
`roles` = sorted union of the membership role and the account role scopes, and
`sid` = this refresh token's `jti` (which is what makes O11 mark the line
`current`). A tenant user with **zero** active memberships is a 401 and gets no
token at all.

Platform-staff logins (v1.7.1, §4.5) MUST carry a live `mfa_code` against a
CONFIRMED enrollment: missing/invalid → **403** `mfa_required` (+ `enrolled`
to say whether the caller should confirm or run `/auth/mfa/setup/` first).
superuser break-glass accounts are deliberately NOT gated here — the support
surface itself rejects them. Rotation is intentionally OFF (access 15 min,
refresh 7 d).""",
        auth=[],
        request=LoginRequestSerializer,
        responses={
            200: LoginResponseSerializer,
            401: OpenApiResponse(
                response=ErrorEnvelopeSerializer,
                description="Invalid credentials, or a tenant user with zero active memberships.",
            ),
            403: OpenApiResponse(
                response=MfaRequiredSerializer,
                description="Platform staff without a verified TOTP code.",
            ),
        },
        examples=[LOGIN_REQUEST_EXAMPLE, LOGIN_RESPONSE_EXAMPLE],
    )
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

    @extend_schema(
        tags=[TAG_AUTH],
        operation_id="refresh_token",
        summary="Refresh the access token",
        description="""Stock SimpleJWT endpoint with ONE deliberate override: the tenancy
claims are **re-derived from current DB truth** when the new access is minted,
so a deactivated member's next refresh yields an org-less token (C-14
fail-closed-empty) instead of copying the old org for up to 7 days.

The response stays stock — `{access}` only; the login/refresh key asymmetry is
contractual and must not be "unified". A refresh line revoked via O12 dies
here with **401** `session revoked`; `sid` on the new access points at exactly
this refresh line, and `last_seen_at` is bumped (never un-revoking).""",
        auth=[],
        request=RefreshRequestSerializer,
        responses={
            200: RefreshResponseSerializer,
            401: OpenApiResponse(
                response=ErrorEnvelopeSerializer,
                description="Invalid/expired refresh token, or a session revoked via O12.",
            ),
        },
    )
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


@extend_schema(
    methods=["GET"],
    tags=[TAG_ORG],
    operation_id="list_org_members",
    summary="List the org's members",
    description="""Bare list (no envelope) of every membership row of the caller's org —
inactive rows included, so the owner can see who was soft-off and reactivate
them. Any member may read this; the org comes from the validated `org_id`
claim, never from a query parameter.""",
    request=None,
    responses={200: MemberOutSerializer(many=True), **error_responses(401)},
    examples=[MEMBER_OUT_EXAMPLE],
)
@extend_schema(
    methods=["POST"],
    tags=[TAG_ORG],
    operation_id="invite_org_member",
    summary="Invite (or reactivate) a member — owner only",
    description="""Owner-exclusive. An unknown `username` is created as a user with an
**unusable password** (login only ever happens through an explicit credential
handover — the invite itself never sets one).

If a membership row already exists for that user in this org:
`is_active: true` → **409**; `is_active: false` → the row is reactivated and
**200** is returned instead of 201. A fresh membership → **201**.

`role` is restricted to the contract enum; anything else is a 400.""",
    request=MemberInviteRequestSerializer,
    responses={
        200: MemberOutSerializer,
        201: MemberOutSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(400, 401, 403),
    },
    examples=[MEMBER_INVITE_EXAMPLE, MEMBER_OUT_EXAMPLE],
)
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


@extend_schema(
    tags=[TAG_ORG],
    operation_id="deactivate_org_member",
    summary="Deactivate a member — owner only",
    description="""Soft-off, never delete. Consequence chain (Security §4.1b, C-14): the
member's live access tokens die within their 15-minute lifetime, the next
refresh yields an org-less token, and org-scoped reads fail closed empty.

Verdicts: 403 caller is not the owner; 404 no such member **in the caller's
org** (a foreign member id is the same 404 — never an oracle). Returns
`{"is_active": false}`.""",
    parameters=[
        OpenApiParameter(
            "member_id", int, OpenApiParameter.PATH, description="OrgMember row id."
        )
    ],
    request=None,
    responses={
        200: MemberDeactivateResponseSerializer,
        **error_responses(401, 403, 404),
    },
)
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


@extend_schema(
    tags=[TAG_ORG],
    operation_id="update_org_member_role",
    summary="Change a member's role — owner only",
    description="""Owner-exclusive role change (contract v1.7.2). Before this endpoint the
only way a membership changed role was to deactivate and re-invite it, and the
`reviewer` role the v1.5 reports wave defined could not be granted at all.

Verdicts: 400 unknown role; 403 caller is not the owner; 404 no such member
**in the caller's org** (a foreign member id is the same 404 — never an
existence oracle); 409 `member already has this role`, and 409 for a
SELF-CHANGE (an owner who demotes themselves can lock the org out of every
owner-only surface, and nothing in the API could undo it).

Role claims are minted from the membership row at login/refresh, so the change
lands at the member's next refresh — this endpoint revokes their live refresh
lines in the same transaction to force exactly that, leaving the ≤15-minute
access-token tail as the accepted, documented risk (C-14 shape). Writes a
`MEMBER_ROLE_CHANGED` audit row.""",
    parameters=[
        OpenApiParameter(
            "member_id", int, OpenApiParameter.PATH, description="OrgMember row id."
        )
    ],
    request=MemberRoleUpdateRequestSerializer,
    responses={
        200: MemberOutSerializer,
        **error_responses(400, 401, 403, 404),
        409: CONFLICT_RESPONSE,
    },
    examples=[
        OpenApiExample("Read-only reports reviewer", request_only=True, value={"role": "reviewer"})
    ],
)
@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def update_member_role(request, member_id: int):
    """/org/members/{id}/ — owner-exclusive role change (v1.7.2 F37).

    فارسی: تغییرِ نقشِ عضو فقط با مالک؛ تغییرِ نقشِ خودِ مالک ممنوع است تا سازمان
    قفل نشود، و خطوطِ نوسازیِ همان عضو باطل می‌شود تا نقشِ تازه اعمال شود.
    """
    forbidden = _require_owner(request)
    if forbidden is not None:
        return forbidden

    org_id = _claims(request).get("org_id")
    if org_id is None:
        return Response({"error": "organization context required"}, status=400)

    member = (
        OrgMember.objects.select_related("user")
        .filter(id=member_id, org_id=org_id)
        .first()
    )
    if member is None:
        return Response({"error": "no such member in the caller's org"}, status=404)

    payload = request.data if isinstance(request.data, dict) else {}
    role = payload.get("role")
    if role not in ORG_MEMBER_ROLES:
        return Response(
            {"error": "role must be one of " + ", ".join(ORG_MEMBER_ROLES)}, status=400
        )

    if member.user_id == request.user.id:
        return Response(
            {"error": "an owner cannot change their own role"}, status=409
        )
    if member.role == role:
        return Response({"error": "member already has this role"}, status=409)

    previous = member.role
    with transaction.atomic():
        member.role = role
        member.save(update_fields=["role"])
        # Role claims are frozen at token birth (permissions.RequireRole reads
        # the claims, never the DB), so the change only lands on a fresh token:
        # kill the member's live refresh lines and let them log in again. The
        # already-issued access token keeps the old role until it expires — the
        # same accepted ≤15-minute window as C-14 deactivation.
        revoked = UserSession.objects.filter(
            user=member.user, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        AccountActivityLog.objects.create(
            actor=request.user,
            org_id=org_id,
            action="MEMBER_ROLE_CHANGED",
            ip_address=request.META.get("REMOTE_ADDR"),
            details={
                "member_id": member.id,
                "user_id": member.user_id,
                "old_role": previous,
                "new_role": role,
                "sessions_revoked": revoked,
            },
        )
    return Response(_member_out(member))


@extend_schema(
    tags=[TAG_SUPPORT],
    operation_id="open_support_session",
    summary="Open an audited org-context support session",
    description="""Platform staff **only** — and never a superuser (the break-glass System
Admin account is deliberately rejected here; a forged `support_session` claim
on a superuser token must not open this surface).

Policies, in order: (1) staff-not-superuser, else 403; (2) `incident_id` is
MANDATORY — blank is a 400; (3) `org_id` must be a real organization UUID,
otherwise 400; (4) a `SUPPORT_ORG_ENTER` audit row is written **before** the
successful return, so an op can never be skipped by a later error.

The returned token lives 900 seconds and carries `org_id` +
`support_session: true` + `incident_id`; its holder is an ordinary
RLS-scoped user of exactly that org — no privilege widens.""",
    request=SupportSessionRequestSerializer,
    responses={
        200: SupportSessionResponseSerializer,
        **error_responses(400, 401, 403),
    },
    examples=[SUPPORT_SESSION_REQUEST_EXAMPLE, SUPPORT_SESSION_RESPONSE_EXAMPLE],
)
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


@extend_schema(
    tags=[TAG_SUPPORT],
    operation_id="setup_staff_mfa",
    summary="(Re)issue the staff TOTP enrollment",
    description="""Staff-not-superuser only. Issues an **unconfirmed** TOTP enrollment and
returns the secret plus the `otpauth://` URI for the authenticator app.

Until `/auth/mfa/confirm/` verifies a live code, platform-staff login stays
blocked (`mfa_required`) — enroll-then-login is the intended first-run flow.
Calling setup again re-issues the secret; a previously confirmed enrollment is
not silently trusted.""",
    request=None,
    responses={
        200: MfaSetupResponseSerializer,
        **error_responses(401, 403),
    },
    examples=[MFA_SETUP_RESPONSE_EXAMPLE],
)
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


@extend_schema(
    tags=[TAG_SUPPORT],
    operation_id="confirm_staff_mfa",
    summary="Confirm the staff TOTP enrollment",
    description="""Verifies the first live code and flips the enrollment row to confirmed,
which is what unblocks platform-staff login. A missing or wrong code is a
plain 400 and stays retryable — the surface is not rate-limited into lockout
by a single typo, but the login path still requires a live code every time.""",
    request=MfaConfirmRequestSerializer,
    responses={
        200: MfaConfirmResponseSerializer,
        **error_responses(400, 401, 403),
    },
    examples=[
        OpenApiExample("Confirm enrollment", request_only=True, value={"code": "492013"})
    ],
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


@extend_schema(
    tags=[TAG_SUPPORT],
    operation_id="support_gateway_provisioning",
    summary="Suspend or resume a gateway in the support session",
    description="""Runbook op inside the audited session org. `action=suspend` flips
PROVISIONED → SUSPENDED and revokes the gateway's EMQX ACLs (traffic stops);
`resume` restores the prior status and re-syncs the allows — the same service
state matrix as the provisioning surface, plus a `SUPPORT_OP_GATEWAY_PROVISIONING`
audit row carrying the session's incident id.

409 when the state matrix rejects the transition (no partial write); a gateway
not in the session org is a 404 `gateway not found in the session org`.""",
    parameters=[
        OpenApiParameter(
            "gateway_slot_id", str, OpenApiParameter.PATH, description="Gateway slot id inside the session org."
        )
    ],
    request=SupportGatewayProvisioningRequestSerializer,
    responses={
        200: SupportGatewayProvisioningResponseSerializer,
        409: CONFLICT_RESPONSE,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Suspend the gateway",
            request_only=True,
            value={"action": "suspend", "reason": "Stuck publish loop, INC-2041"},
        )
    ],
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


@extend_schema(
    tags=[TAG_SUPPORT],
    operation_id="support_alert_force_resolve",
    summary="Force-resolve an alert stuck by a known engine defect",
    description="""Runbook-only escape hatch: writes `resolved_at` **directly** (normal
resolution stays hysteresis-driven). `reason` is mandatory — a blank value is
a 400 — because the audit row must name the defect reference.

Idempotent: an already-resolved alert returns the same 200 shape with
`idempotent: true` and writes nothing new. Foreign/unknown alert → 404 in the
session org.""",
    parameters=[
        OpenApiParameter(
            "alert_id", str, OpenApiParameter.PATH, description="Alert uuid inside the session org."
        )
    ],
    request=SupportForceResolveRequestSerializer,
    responses={
        200: SupportForceResolveResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Force resolve",
            request_only=True,
            value={"reason": "NH3 velocity latch stuck (defect ENG-118), INC-2041"},
        )
    ],
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

    # BUG-9b full lifecycle: clearing the coalesce blob is what lets the
    # metric page again; a force-resolved alert that leaves `alert:active:*`
    # behind blackholes every future episode (QA 8.1/8.2b/8.8). Redis down
    # must not fail the runbook op, so the clear is best-effort.
    # فارسی: پاک‌سازیِ اشاره‌گرِ فعال تا سنجه دوباره هشدار بدهد؛ خطای ردیس
    # نباید عملیاتِ بستنِ اجباری را بشکند.
    try:
        from apps.alerts.services.firing import clear_active_blob
        from apps.alerts.services.sweeps import system_redis

        clear_active_blob(system_redis(), alert.saloon_id, alert.metric_type)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "force-resolve active-pointer clear degraded"
        )

    _audit_support_op(
        request, org_id, "SUPPORT_OP_ALERT_FORCE_RESOLVE", incident_id,
        {"alert_id": str(alert.pk), "reason": reason},
    )
    return Response({"alert_id": str(alert.pk), "resolved": True})


@extend_schema(
    tags=[TAG_SUPPORT],
    operation_id="support_task_dispatch",
    summary="Dispatch a whitelisted maintenance task",
    description="""Only three tasks exist (`drain_pending_approval`,
`rebuild_threshold_cache`, `rebuild_zone_cache`) — anything else is a 400
`unknown task`; `reason` is mandatory. `drain_pending_approval` additionally
requires a `gateway_slot_id` belonging to the session org (404 otherwise).

**202 Accepted**: the work runs on the worker with the session org's scope
passed explicitly; the response echoes the task and what was dispatched. Every
dispatch writes a `SUPPORT_OP_TASK_DISPATCH` audit row with the session's
incident id.""",
    request=SupportTaskDispatchRequestSerializer,
    responses={
        202: SupportTaskDispatchResponseSerializer,
        **error_responses(400, 401, 403, 404),
    },
    examples=[
        OpenApiExample(
            "Drain a gateway's parked requests",
            request_only=True,
            value={
                "task": "drain_pending_approval",
                "gateway_slot_id": "a1b2c3d4-0000-4000-8000-000000000001",
                "reason": "Installer confirmed the farm, INC-2041",
            },
        ),
        OpenApiExample(
            "Dispatched",
            response_only=True,
            value={"task": "drain_pending_approval", "dispatched": "drain dispatched for a1b2c3d4-…"},
        ),
    ],
)
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
