"""JWT authentication for DRF — the verify-once contract (Security §4.1, D3).

WHY: the canon says the MIDDLEWARE validates the Bearer token exactly once per
request and stashes the result; this class reuses that stash so signature
verification never happens twice. When no middleware ran (management shell,
direct serializer tests, or a misordered stack), it falls back to stock SimpleJWT
verification — same rules, just slower. Fail-closed is preserved either way:
no/invalid header ⇒ AuthenticationFailed ⇒ 401.

فارسی: احراز هویتِ یک‌باره — اگر میان‌افزار توکن را تأیید و ذخیره کرده باشد از همان
استفاده می‌کنیم؛ وگرنه تأیید استاندارد انجام می‌شود. در هر دو حالت درخواستِ بدون
توکنِ معتبر خطای ۴۰۱ می‌گیرد.
"""

from rest_framework_simplejwt.authentication import JWTAuthentication

# Where TenantOrgMiddleware (M2-05) parks (user, validated_token) after its
# single verification. Underscore = internal contract between the two layers.
JWT_AUTH_STASH_ATTR = "_jwt_cached_auth"


class CachedJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        stash = getattr(request, JWT_AUTH_STASH_ATTR, None)
        if stash is not None:
            return stash  # already verified once upstream — reuse, never re-sign-check
        return super().authenticate(request)
