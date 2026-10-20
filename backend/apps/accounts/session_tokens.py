"""Scoped platform sessions — one issuer, two kinds (AD-14, 2026-10-18).

WHY this module exists: platform staff (the software company's own Hardware
Team, Support, and now Installers) are deliberately ORG-LESS. The tenant
middleware's branch ② sends them through with NO GUC
(`apps/tenants/middleware.py`), so every tenant table returns zero rows by
design. To work inside one customer organization they must present a token that
carries that org — which is exactly what the audited support session already
does (`accounts/views.py::support_session`).

AD-14 adds a second kind on the same mechanism: an INSTALLER session, scoped to
one installation job's org AND farm. The two kinds share the issuer but NOT the
claim name:

    support_session   → also unlocks `_support_op_context` (gateway
                        suspend/resume, alert force-resolve)
    installer_session → does NOT. An installer must never inherit a support
                        operator's powers.

Keeping them distinct is the whole reason this is a `kind` argument rather than
a boolean. Do not collapse them into one claim.

فارسی: یک صادرکننده برای دو نوع نشست؛ ادعای نصاب عمداً از ادعای پشتیبانی جدا
است تا نصاب هرگز به اختیارات اپراتور پشتیبانی دست پیدا نکند.
"""

from datetime import timedelta

from django.utils import timezone

# 15 minutes, matching the support-session policy (Security §4.5). A field
# visit outlives it on purpose: the client re-opens silently, which keeps the
# window short if a phone is lost on site.
SESSION_TTL_SEC = 900

KIND_SUPPORT = "support"
KIND_INSTALLER = "installer"


def issue_scoped_session(user, *, org_id, kind, extra_claims=None):
    """Mint a 15-minute access token scoped to one org.

    Returns the encoded token string. The caller is responsible for writing its
    own audit row BEFORE returning the token (both existing callers do).

    Claims added on top of the user's normal token:
      org_id            — the GUC source for middleware branch ①/①b
      <kind>_session    — `support_session` or `installer_session`
      incident_id       — support only (audit anchor, mandatory at the view)
      job_id / farm_id  — installer only (the scope of the visit)
    """
    from rest_framework_simplejwt.tokens import AccessToken

    if kind not in (KIND_SUPPORT, KIND_INSTALLER):
        raise ValueError(f"unknown session kind: {kind!r}")

    token = AccessToken.for_user(user)
    token["org_id"] = str(org_id)
    token[f"{kind}_session"] = True
    token.set_exp(lifetime=timedelta(seconds=SESSION_TTL_SEC))
    for key, value in (extra_claims or {}).items():
        if value is not None:
            token[key] = value
    return str(token)


def session_expiry_iso():
    """Expiry timestamp for the response body, so clients can pre-empt 401s."""
    return (timezone.now() + timedelta(seconds=SESSION_TTL_SEC)).isoformat()
