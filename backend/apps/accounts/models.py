"""
Accounts — identity, membership, role scoping (database.md Compartments 1–2).

فارسی: هویت کاربر، عضویت سازمانی و دامنهٔ نقش‌ها — ترنسکریپتِ عیناً از database.md.

The doc's "ALTER TABLE auth_user ADD COLUMN is_platform_staff" is realized as
a custom AUTH_USER_MODEL (CONSTRAINTS C-09): subclassing is the Django-
canonical way to extend the user table so the extra column exists under BOTH
real migrations and the --nomigrations model-state synthesis used by tests.
Doc references to `auth_user` resolve to this table.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import F, Value
from django.db.models.functions import Coalesce, Now


class User(AbstractUser):
    id = models.AutoField(primary_key=True)  # doc: auth_user.id INT (serial)
    # GS-fix 2026-08-22 platform-support path flag; BYPASSRLS deliberately NOT
    # granted at the DB level (Security §4.5).
    is_platform_staff = models.BooleanField(default=False)
    # M5-02: platform review role — "admin"/"support" are the DEFAULT
    # reviewers per TASKS M5-02; `_can_review` in apps/provisioning/api.py
    # reads this axis (superuser always passes). NULL = no platform role.
    staff_role = models.CharField(max_length=30, null=True, blank=True)

    class Meta:
        db_table = "accounts_user"


class PlatformStaffMFA(models.Model):
    """M7-07: staff MFA enrollment (Security Architecture §4.5 activation).

    One row per platform-staff user; `secret` is the TOTP shared secret in
    pyotp-native base32, `confirmed_at` flips only after the first live code
    verifies — a two-step enroll so a typo'd secret never locks the on-call
    engineer out. Login refuses platform-staff users without a CONFIRMED row.
    Tenant accounts get no row: tenant MFA is Phase 2 (SEC-MFA).
    """

    user = models.OneToOneField(
        "accounts.User", on_delete=models.CASCADE, related_name="platform_mfa"
    )
    secret = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "platform_staff_mfa"


class OrgMember(models.Model):
    id = models.AutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, null=True, related_name="+")
    user = models.ForeignKey(User,

        db_column="user", on_delete=models.RESTRICT, null=True, related_name="+")
    role = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True, null=True)
    joined_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "org_members"
        constraints = [
            models.UniqueConstraint(fields=["org", "user"], name="uq_org_members_org_user"),
        ]
        indexes = [
            # Doc: "Index for Django middleware tenant resolution" (v0.2 middleware).
            models.Index(fields=["user"], name="idx_org_members_user"),
        ]


class AccountRoleScope(models.Model):
    """Role scoping with NULL = access to ALL resources at that level.

    province_id (2026-08-23, Tracker §4i): regional scoping for the Device
    Installer — farm_id NULL + saloon_id NULL + province_id set sees only
    farms in that province; NULL province = unrestricted within the org.
    """

    id = models.AutoField(primary_key=True)
    org = models.ForeignKey("tenants.Organization", on_delete=models.RESTRICT, null=True, related_name="+")
    user = models.ForeignKey(User,

        db_column="user", on_delete=models.RESTRICT, null=True, related_name="+")
    role = models.CharField(max_length=50)
    province = models.ForeignKey("geography.Province", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    farm = models.ForeignKey("farms.Farm", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    saloon = models.ForeignKey("farms.Saloon", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    assigned_at = models.DateTimeField(null=True, db_default=Now())

    class Meta:
        db_table = "account_role_scopes"
        constraints = [
            # Verbatim COALESCE unique from the doc — functional unique constraint.
            models.UniqueConstraint(
                Coalesce(F("province"), Value(-1)),
                Coalesce(F("farm"), Value(-1)),
                Coalesce(F("saloon"), Value(-1)),
                F("org"),
                F("user"),
                F("role"),
                name="uq_account_role_scopes_scope",
            ),
        ]


class UserSession(models.Model):
    """M6-10 (contract v1.7.0 §7 design): one row per live refresh token.

    WHY: the O11/O12 session list/revoke contract needs a per-login register,
    and the token_blacklist app is DELIBERATELY never installed (base.py: the
    blacklist would promise revocation v0.x does not implement). This table is
    the register instead:

    - refresh_jti (unique) keys the row; O12 revocation flips revoked_at and
      the RefreshView override then refuses any refresh whose jti is revoked —
      the "refresh line dies" of the contract, sized at one cheap indexed
      lookup per refresh (15-min access expiry still applies, as contracted).
    - ip / user_agent come from the login/refresh request (best-effort strings
      — a proxy chain is not parsed here, REMOTE_ADDR is honest).
    - last_seen_at updates on every successful refresh; `current` in O11 is
      derived per-request (never stored).

    فارسی: یک ردیف به‌ازای هر توکنِ نوسازی زنده؛ ابطال یعنی ست‌شدنِ revoked_at و
    ردِ نوسازی بعدی بر اساس jti — بدونِ نصبِ اپِ blacklist که همیشه ممنوع بود.
    """

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey("User", on_delete=models.RESTRICT, related_name="+")
    refresh_jti = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(null=True, db_default=Now())
    last_seen_at = models.DateTimeField(null=True, db_default=Now())
    ip = models.GenericIPAddressField(null=True, blank=True)  # INET on postgres
    user_agent = models.CharField(max_length=400, null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "user_sessions"
        indexes = [
            models.Index(fields=["user", "revoked_at"], name="idx_user_sessions_user"),
        ]
