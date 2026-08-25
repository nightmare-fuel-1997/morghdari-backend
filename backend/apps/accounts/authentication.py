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

# TenantOrgMiddleware (M2-05) parks the ALREADY-VALIDATED token here after its
# single signature check; this class then only resolves the user. Underscore =
# internal contract between the two layers.
TOKEN_STASH_ATTR = "_jwt_validated_token"


class CachedJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        validated = getattr(request, TOKEN_STASH_ATTR, None)
        if validated is not None:
            # Signature already verified once upstream — resolve user, never
            # re-check the signature.
            return self.get_user(validated), validated
        return super().authenticate(request)
