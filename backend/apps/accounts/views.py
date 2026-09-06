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

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts.models import OrgMember
from apps.accounts.services import (
    register_session,
    refresh_is_revoked,
    touch_session,
)
from apps.accounts.permissions import _claims
from apps.accounts.serializers import LoginSerializer, resolve_active_membership
from apps.auditing.models import AccountActivityLog
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

    org = Organization.objects.filter(org_id=request.data.get("org_id") or "").first()
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
