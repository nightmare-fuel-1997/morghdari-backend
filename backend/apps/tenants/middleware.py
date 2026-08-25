"""TenantOrgMiddleware — one verified claim set, one GUC, one transaction.

This is the HTTP twin of v0.1's tenant_context() (Security Architecture §4.1,
decisions D2/D3, Tracker §4j). NORMATIVE branch order — evaluated on VALIDATED
TOKEN CLAIMS before any user flags:

  ① support_session claim true AND org_id present → GUC from that org_id
    (runs REGARDLESS of is_platform_staff — §4.5 support sessions)
  ② elif is_platform_staff claim true → skip: NO GUC, FORCE RLS yields zero
    tenant rows (fail-closed, NOT a backdoor; BYPASSRLS alias stays out of
    v0.2 scope, parked Tracker §6)
  ③ elif org_id claim present → GUC (ordinary tenant request)
  ④ else pass-through untouched (anonymous, rejected token, org-less token)

Verify-once (D3): the Bearer token is signature-checked EXACTLY once here;
the validated token is stashed for CachedJWTAuthentication so DRF never
re-verifies. On TokenError the rejection is LOGGED (NR-4) with reason, no
stash is left behind, and the request passes through — protected views then
401 at the DRF layer (fail-closed, not 500).

Transaction topology: the GUC AND get_response live INSIDE one
transaction.atomic() — the whole view executes within the transaction holding
the GUC (canon §4.1; TUTORIAL §3.3 echo). Transaction-scoped set_config dies
at COMMIT; the NULLIF policy template (C-12) makes the empty-string residue
harmless on pooled connections.

فارسی: میان‌افزارِ تنس — ترتیب شاخه‌ها نُرماتیو است و روی ادعاهای تأییدشدهٔ توکن
ارزیابی می‌شود: اول نشست پشتیبانی، بعد کارکنان پلتفرم، بعد ادعای سازمان، وگرنه
عبور بی‌رودخواست. امضای توکن فقط یک بار بررسی و برای DRF ذخیره می‌شود؛ GUC و
اجرای ویو درون یک تراکنش می‌مانند تا با COMMIT بمیرد.
"""

import logging

from django.db import connection, transaction
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

logger = logging.getLogger(__name__)

# Where the validated token waits for CachedJWTAuthentication (verify-once).
TOKEN_STASH_ATTR = "_jwt_validated_token"
CLAIMS_ATTR = "jwt_claims"


class TenantOrgMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        claims = None
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Bearer "):
            try:
                token = AccessToken(auth_header.removeprefix("Bearer ").strip())
            except TokenError as exc:
                # NR-4: never swallow silently — triage needs the reason, but
                # never log the token material itself.
                logger.warning("JWT rejected at tenant middleware: %s", exc)
            else:
                claims = token.payload
                setattr(request, TOKEN_STASH_ATTR, token)

        if claims is None:
            return self.get_response(request)  # ④ rejected / absent token

        setattr(request, CLAIMS_ATTR, dict(claims))

        org_id = None
        if claims.get("support_session") is True and claims.get("org_id"):
            org_id = claims["org_id"]  # ① support session wins over the staff skip
        elif claims.get("is_platform_staff") is True:
            return self.get_response(request)  # ② platform staff: no GUC, zero rows
        elif claims.get("org_id"):
            org_id = claims["org_id"]  # ③ ordinary tenant request
        else:
            return self.get_response(request)  # ④ authenticated but org-less

        with transaction.atomic():
            with connection.cursor() as cursor:
                # Parameterized + tx-scoped (is_local=true): dies with COMMIT;
                # identical idiom to tenants.context.tenant_context().
                cursor.execute(
                    "SELECT set_config('app.current_org_id', %s, true)", [org_id]
                )
            # INSIDE the atomic block on purpose: the whole view must execute
            # within the transaction that holds the GUC (Security §4.1).
            return self.get_response(request)
