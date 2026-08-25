"""Login claims — Security Architecture §4.1b, frozen at token birth (M2-02).

WHY: the JWT is the ONLY carrier of tenant identity in v0.2's HTTP path, so
claim semantics are pinned doc-first and must not drift in code:
- org = the caller's single ACTIVE org_members row, ordered (joined_at, id)
  ASC — multi-org users deterministically get their oldest active membership;
  zero active memberships → AuthenticationFailed → HTTP 401 (no token).
- roles[] = sorted union of org_members.role ∪ account_role_scopes.role for
  THAT org (contract LoginResponse example shows ["owner", "doctor"] — a
  doctor role only exists in account_role_scopes, hence the union).
- is_platform_staff travels separately from roles: platform identity and
  tenant identity are different axes (D15); the flag is OMITTED when False so
  tokens stay minimal.
An optional LoginRequest.org_id selector is PARKED (Tracker §6) — do not add
it here without a new written decision.

فارسی: ادعاهای توکن هنگام صدور منجمد می‌شوند — سازمانِ فعالِ قدیمی‌ترین عضویت،
نقش‌ها اجتماعِ مرتب‌شدهٔ نقش پایه و دامنه‌های نقش، و پرچم کارکنان پلتفرم جدا از
نقش‌ها؛ عضویت فعال صفر یعنی خطای ۴۰۱ و بدون توکن.
"""

from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.accounts.models import AccountRoleScope, OrgMember


class LoginSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        member = (
            OrgMember.objects.filter(user=user, is_active=True)
            .order_by("joined_at", "id")  # deterministic pick: OLDEST active membership wins
            .first()
        )
        if member is None:
            # Zero active memberships → 401 at the view layer; never a token
            # with an empty org claim (that would silently read zero rows and
            # mask a provisioning bug as an empty farm).
            raise AuthenticationFailed("no active org membership")
        token["org_id"] = str(member.org_id)
        extra_roles = set(
            AccountRoleScope.objects.filter(user=user, org=member.org).values_list("role", flat=True)
        )
        token["roles"] = sorted({member.role} | extra_roles)  # sorted set-union — deterministic
        if user.is_platform_staff:
            token["is_platform_staff"] = True
        return token
