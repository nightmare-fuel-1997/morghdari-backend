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

from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts.serializers import LoginSerializer, resolve_active_membership


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
