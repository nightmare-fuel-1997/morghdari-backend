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
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts.models import OrgMember
from apps.accounts.permissions import _claims
from apps.accounts.serializers import LoginSerializer, resolve_active_membership

# Contract v1.4.3 OrgMemberInviteRequest.role enum — the ONLY writable roles.
ORG_MEMBER_ROLES = ["owner", "manager", "engineer", "doctor", "worker", "installer"]

User = get_user_model()


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member, roles = resolve_active_membership(serializer.user)
        return Response(
            {
                "access_token": serializer.validated_data["access"],
                "refresh_token": serializer.validated_data["refresh"],
                "user": {
                    "id": serializer.user.id,
                    "username": serializer.user.username,
                    "roles": roles,
                    "org_id": str(member.org_id),
                    "org_name": member.org.name,
                    "is_platform_staff": bool(serializer.user.is_platform_staff),
                },
            }
        )


class RefreshView(TokenRefreshView):
    """Stock on purpose: contract RefreshResponse = native `{access}` only."""


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
