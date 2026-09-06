"""Session register services — M6-10 (contract v1.7.0 O11/O12, §7 design).

WHY a table (not Redis, not SimpleJWT blacklist): O11/O12 need a durable,
per-user LISTABLE register with IP/agent provenance, and the token_blacklist
app is deliberately NEVER installed (base.py: a blacklist app would promise
revocation v0.x does not implement). user_sessions IS that register:

- register_session() runs inside login/refresh AFTER a refresh token exists;
  failure must NEVER break login (best-effort, defensively wrapped).
- touch_session() refreshes last_seen_at on every successful refresh.
- refresh_is_revoked() is the one hot-path check: O12-revoked jti ⇒ the next
  refresh dies (contract: "that refresh line dies; access token inside its
  15-min expiry").

فارسی: جدولِ user_sessions همان ثبتِ نشست‌هاست؛ نویسنده‌ها بهترین‌تلاش‌اند و
هیچ‌وقت لاگین را نمی‌شکنند، و jtiِ ابطال‌شده در نوسازی بعدی رد می‌شود.
"""

import logging

from django.utils import timezone

from apps.accounts.models import UserSession

logger = logging.getLogger(__name__)


def register_session(user, refresh_token, request) -> None:
    """Best-effort row insert for a freshly minted refresh token.

    Reads the request ONLY for provenance (REMOTE_ADDR / UA header); never
    stores token material beyond the already-public jti claim.
    """
    try:
        jti = refresh_token.payload.get("jti")
        if not jti:
            return
        UserSession.objects.update_or_create(
            refresh_jti=jti,
            defaults={
                "user": user,
                "revoked_at": None,  # re-login with the same jti un-revokes
                "ip": request.META.get("REMOTE_ADDR") or None,
                "user_agent": (request.META.get("HTTP_USER_AGENT") or "")[:400] or None,
            },
        )
    except Exception:  # noqa: BLE001 — a broken register never breaks login
        logger.warning("user_sessions register failed", exc_info=True)


def touch_session(refresh_token) -> None:
    """Best-effort last_seen_at bump on a successful refresh."""
    try:
        jti = refresh_token.payload.get("jti")
        if not jti:
            return
        UserSession.objects.filter(refresh_jti=jti, revoked_at__isnull=True).update(
            last_seen_at=timezone.now()
        )
    except Exception:  # noqa: BLE001
        logger.warning("user_sessions touch failed", exc_info=True)


def refresh_is_revoked(refresh_token) -> bool:
    """Hot-path O12 check: True ⇒ RefreshView must refuse this refresh.

    The register keys on the REFRESH token's jti (rows are written from the
    login/refresh response where the refresh token is in hand) — so the check
    reads jti directly; a token with neither claim predates M6-10 (or is a
    support token) and is simply outside the register.
    """
    jti = refresh_token.payload.get("sid") or refresh_token.payload.get("jti")
    if not jti:
        return False
    return UserSession.objects.filter(refresh_jti=jti, revoked_at__isnull=False).exists()
