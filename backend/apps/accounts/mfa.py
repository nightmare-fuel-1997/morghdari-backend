"""MFA for platform-staff accounts — Security Architecture §4.5 (v0.7 M7-07).

Policy (exact scope of the doc, nothing wider):
- Platform-staff accounts MUST have MFA before the support surface is usable —
  enforced at LOGIN (a staff login without a CONFIRMED enrollment is rejected
  403 mfa_required) and at the support-session opener.
- Enrollment is a two-step dance: POST mfa/setup returns the secret + otpauth
  URI; POST mfa/confirm verifies the first live code and flips confirmed_at.
  Until confirmed, login stays blocked for that staff user (enroll-then-login
  is the intended flow for account creation by System Admin).
- Tenant accounts deliberately get NO MFA fields — the SEC-MFA amendment keeps
  tenant MFA at Phase 2, so the login gate keys on is_platform_staff ONLY.
- superuser is intentionally NOT gated: the System Admin path is the break-glass
  account and the docs' "superuser untouched" line stays true. The support
  surface itself rejects superusers, so gating them adds lockout risk with no
  security win.

Secrets are pyotp-NATIVE base32 (random_base32) so every pyotp call works
without conversion; recovery codes are Phase 2 (SEC-MFA) — losing the device
means a System Admin deletes the enrollment row (audited like every staff
mutation).

فارسی: MFA فقط برای حساب‌های پلتفرمی الزامی است؛ ورود پرسنلِ پلتفرم بدون ثبتِ
تأییدشده بسته است و ثبت نام دو مرحله‌ای است تا کد اشتباه کاربر را قفل نکند.
"""

from django.utils import timezone
import pyotp

from apps.accounts.models import PlatformStaffMFA

WINDOW = 1  # ±30s clock skew tolerance (RFC 6238 recommendation)
ISSUER = "Morghdari"


def _totp(secret: str) -> pyotp.TOTP:
    return pyotp.TOTP(secret)


def provision(user) -> PlatformStaffMFA:
    """(Re)issue an UNCONFIRMED enrollment for the staff user. Called on
    mfa/setup; idempotent in effect — each call replaces the pending secret
    (an abandoned enrollment can never half-exist)."""
    secret = pyotp.random_base32()
    row, _ = PlatformStaffMFA.objects.update_or_create(
        user=user,
        defaults={"secret": secret, "confirmed_at": None},
    )
    return row


def provisioning_uri(user, secret: str) -> str:
    return _totp(secret).provisioning_uri(name=user.username, issuer_name=ISSUER)


def confirm(user, code) -> bool:
    """Verify the first live code and mark the enrollment CONFIRMED. A wrong
    code leaves the row unconfirmed (retry allowed) — and login stays blocked."""
    row = PlatformStaffMFA.objects.filter(user=user).first()
    if row is None or row.confirmed_at is not None:
        return False
    if not _totp(row.secret).verify(str(code).strip(), valid_window=WINDOW):
        return False
    row.confirmed_at = timezone.now()
    row.save(update_fields=["confirmed_at"])
    return True


def is_confirmed(user) -> bool:
    return PlatformStaffMFA.objects.filter(
        user=user, confirmed_at__isnull=False
    ).exists()


def verify_login_code(user, code) -> bool:
    """The login-time check. Missing row or missing code ⇒ False (the caller
    turns that into 403 mfa_required with an enrollment pointer)."""
    if code is None or not str(code).strip():
        return False
    row = PlatformStaffMFA.objects.filter(user=user).first()
    if row is None or row.confirmed_at is None:
        return False
    return _totp(row.secret).verify(str(code).strip(), valid_window=WINDOW)
