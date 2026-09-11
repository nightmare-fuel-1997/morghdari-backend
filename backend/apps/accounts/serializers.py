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

from drf_spectacular.utils import OpenApiExample
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.accounts.models import AccountRoleScope, OrgMember


class LoginSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        member, roles = resolve_active_membership(user)
        token["roles"] = roles  # sorted set-union — deterministic (staff: empty)
        if member is not None:
            token["org_id"] = str(member.org_id)
        if user.is_platform_staff:
            token["is_platform_staff"] = True
        # M6-10 (O11): sid = this refresh token's jti, stamped BEFORE the
        # access is derived — SimpleJWT copies non-standard claims onto the
        # paired access, so the O11 `current` line matches on first login
        # (no extra round-trip; the register row keys on the same jti).
        token["sid"] = token.payload.get("jti")
        return token


def resolve_active_membership(user):
    """Single source of truth for WHO the token says you are (Security §4.1b).

    Returns (member, roles) for the caller's oldest ACTIVE membership, or
    (None, []) for platform staff — the exception axis (D15): staff tokens are
    ORG-LESS (no org_id claim ⇒ middleware branch ② skip-GUC), which is what
    lets them reach /platform/support/session/ at all. Raises
    AuthenticationFailed on a tenant user with zero active memberships so both
    token issuance and the profile envelope fail the same way — one rule, two
    consumers.
    فارسی: تنها منبع حقیقت هویتِ توکن؛ کارکنان پلتفرم توکنِ بدونِ سازمان می‌گیرند
    (محورِ جدا، D15)، و عضویت فعالِ صفر برای کاربر عادی یعنی خطای ۴۰۱.
    """
    if user.is_platform_staff:
        return None, []
    member = (
        OrgMember.objects.filter(user=user, is_active=True)
        .order_by("joined_at", "id")  # deterministic pick: OLDEST active membership wins
        .first()
    )
    if member is None:
        # Zero active memberships → 401 at the view layer; never a token with
        # an empty org claim (that would silently read zero rows and mask a
        # provisioning bug as an empty farm).
        raise AuthenticationFailed("no active org membership")
    extra_roles = set(
        AccountRoleScope.objects.filter(user=user, org=member.org).values_list("role", flat=True)
    )
    return member, sorted({member.role} | extra_roles)


# Docs-only DRF mirrors for @extend_schema (never used at runtime).
# Each mirrors the dict the view builds (_member_out / the support envelopes)
# and backend/openapi.yaml examples, so the generated schema cannot drift from
# what the endpoints actually return (contract v1.4.3 + M7-07).
# ---------------------------------------------------------------------------


class LoginRequestSerializer(serializers.Serializer):
    """LoginRequest — contract v1.4.3 + the v1.7.1 staff MFA field."""

    username = serializers.CharField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    mfa_code = serializers.CharField(
        required=False,
        help_text=(
            "Live TOTP code — REQUIRED for platform-staff accounts (v1.7.1, M7-07). "
            "Tenant accounts omit it."
        ),
    )


class UserProfileSerializer(serializers.Serializer):
    """The `user` envelope — reuses the claim rule, so it cannot disagree with the token."""

    id = serializers.IntegerField()
    username = serializers.CharField()
    roles = serializers.ListField(
        child=serializers.ChoiceField(
            choices=["owner", "manager", "engineer", "doctor", "worker", "installer", "reviewer"]
        ),
        help_text="Sorted union of the membership role and the account role scopes.",
    )
    org_id = serializers.CharField(
        allow_null=True,
        required=False,
        help_text="Null for platform staff — staff tokens are org-less by design (D15).",
    )
    org_name = serializers.CharField(allow_null=True, required=False)
    is_platform_staff = serializers.BooleanField()


class LoginResponseSerializer(serializers.Serializer):
    """LoginResponse — contract keys, NOT SimpleJWT's stock `{access, refresh}`."""

    access_token = serializers.CharField()
    refresh_token = serializers.CharField()
    user = UserProfileSerializer()


class MfaRequiredSerializer(serializers.Serializer):
    """403 body for a platform-staff login without a valid TOTP code."""

    error = serializers.CharField(help_text='Always "mfa_required".')
    detail = serializers.CharField(
        help_text='Enrollment hint: verify a code, or run /auth/mfa/setup/ when unenrolled.'
    )
    enrolled = serializers.BooleanField(help_text="Whether a confirmed enrollment exists.")


class RefreshRequestSerializer(serializers.Serializer):
    """RefreshRequest — the stock key name `refresh` is contractual."""

    refresh = serializers.CharField(help_text="The refresh token issued at login.")


class RefreshResponseSerializer(serializers.Serializer):
    """RefreshResponse — stock `{access}` ONLY (the asymmetry with login is contractual).

    The new access is minted from CURRENT DB truth: a deactivated member gets
    an org-less access token instead of their old org's claims (C-14)."""

    access = serializers.CharField(help_text="Fresh access token (15 min).")


class MemberOutSerializer(serializers.Serializer):
    """OrgMemberOut — one row of GET /org/members/ and the invite responses."""

    member_id = serializers.IntegerField(help_text="OrgMember row id.")
    user_id = serializers.IntegerField(help_text="User id (stable across memberships).")
    username = serializers.CharField()
    role = serializers.CharField(help_text="Base org role from the membership row.")
    is_active = serializers.BooleanField(
        help_text="Soft-off flag: deactivated members stay as rows (never deleted)."
    )
    joined_at = serializers.CharField(allow_null=True, required=False, help_text="ISO Z.")


class MemberInviteRequestSerializer(serializers.Serializer):
    """OrgMemberInviteRequest — owner-exclusive invite/reactivate body."""

    username = serializers.CharField(help_text="Existing user is reused; a new one is created with an unusable password.")
    role = serializers.ChoiceField(
        choices=["owner", "manager", "engineer", "doctor", "worker", "installer"]
    )


class MemberDeactivateResponseSerializer(serializers.Serializer):
    is_active = serializers.BooleanField(help_text="Always false — the soft-off result.")


class SupportSessionRequestSerializer(serializers.Serializer):
    """/platform/support/session/ body — staff-not-superuser only."""

    org_id = serializers.UUIDField(help_text="Target organization UUID; unknown values are a 400, never a hint.")
    incident_id = serializers.CharField(
        max_length=64, help_text="MANDATORY audit anchor — a blank value is a 400."
    )
    reason = serializers.CharField(
        required=False, allow_blank=True, help_text="Free-text context stored on the SUPPORT_ORG_ENTER audit row."
    )


class SupportSessionResponseSerializer(serializers.Serializer):
    """Scoped support token — 15 minutes, claims add org_id + support_session + incident_id."""

    access_token = serializers.CharField(help_text="Short-lived, org-scoped Bearer token.")
    org_id = serializers.CharField(help_text="The org the session is scoped to.")
    incident_id = serializers.CharField()
    expires_in_sec = serializers.IntegerField(help_text="Always 900 (15 minutes).")


class MfaSetupResponseSerializer(serializers.Serializer):
    """TOTP enrollment secret; login stays blocked until /mfa/confirm/."""

    secret = serializers.CharField(help_text="Base32/hex secret for the authenticator app.")
    otpauth_uri = serializers.CharField(help_text="otpauth:// URI to scan.")
    confirmed = serializers.BooleanField(help_text="False until a live code is verified.")


class MfaConfirmRequestSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="Current TOTP code from the enrolled device.")


class MfaConfirmResponseSerializer(serializers.Serializer):
    confirmed = serializers.BooleanField(help_text="True once the enrollment row is confirmed.")


class SupportGatewayProvisioningRequestSerializer(serializers.Serializer):
    """Suspend/resume one gateway inside the session org."""

    action = serializers.ChoiceField(choices=["suspend", "resume"])
    reason = serializers.CharField(
        required=False, allow_blank=True, help_text="Stored on the audit row (defaults to 'support suspension')."
    )


class SupportGatewayProvisioningResponseSerializer(serializers.Serializer):
    gateway_slot_id = serializers.CharField(help_text="Gateway the op was applied to.")
    provisioning_status = serializers.CharField(
        help_text="Status after the op: SUSPENDED on suspend, the prior status on resume."
    )


class SupportForceResolveRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(help_text="MANDATORY runbook defect reference — a blank value is a 400.")


class SupportForceResolveResponseSerializer(serializers.Serializer):
    alert_id = serializers.CharField(help_text="Stringified alert uuid.")
    resolved = serializers.BooleanField(help_text="True once the alert is resolved.")
    idempotent = serializers.BooleanField(
        required=False, help_text="Present (true) only when the alert was already resolved."
    )


class SupportTaskDispatchRequestSerializer(serializers.Serializer):
    """Whitelisted maintenance task — anything else is a 400."""

    task = serializers.ChoiceField(
        choices=["drain_pending_approval", "rebuild_threshold_cache", "rebuild_zone_cache"]
    )
    reason = serializers.CharField(help_text="MANDATORY justification — a blank value is a 400.")
    gateway_slot_id = serializers.CharField(
        required=False,
        help_text="Required for drain_pending_approval; must belong to the session org.",
    )


class SupportTaskDispatchResponseSerializer(serializers.Serializer):
    task = serializers.CharField(help_text="Echo of the dispatched task.")
    dispatched = serializers.CharField(help_text="Human-readable outcome of the dispatch.")


LOGIN_REQUEST_EXAMPLE = OpenApiExample(
    "Tenant login",
    request_only=True,
    value={"username": "tom@goldenegg.com", "password": "farm-floor-2026"},
)

LOGIN_RESPONSE_EXAMPLE = OpenApiExample(
    "Login response",
    response_only=True,
    value={
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…",
        "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…",
        "user": {
            "id": 9,
            "username": "tom@goldenegg.com",
            "roles": ["owner", "doctor"],
            "org_id": "1f0c3a54-6f2b-4c9d-9d5e-2b7f4a1c8e30",
            "org_name": "Golden Egg",
            "is_platform_staff": False,
        },
    },
)

MEMBER_INVITE_EXAMPLE = OpenApiExample(
    "Invite an engineer",
    request_only=True,
    value={"username": "sara.engineer", "role": "engineer"},
)

MEMBER_OUT_EXAMPLE = OpenApiExample(
    "Member row",
    response_only=True,
    value={
        "member_id": 18,
        "user_id": 42,
        "username": "sara.engineer",
        "role": "engineer",
        "is_active": True,
        "joined_at": "2026-08-18T09:14:02Z",
    },
)

SUPPORT_SESSION_REQUEST_EXAMPLE = OpenApiExample(
    "Open a support session",
    request_only=True,
    value={"org_id": "1f0c3a54-6f2b-4c9d-9d5e-2b7f4a1c8e30", "incident_id": "INC-2041", "reason": "NH3 sensor stuck"},
)

SUPPORT_SESSION_RESPONSE_EXAMPLE = OpenApiExample(
    "Scoped session token",
    response_only=True,
    value={
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…",
        "org_id": "1f0c3a54-6f2b-4c9d-9d5e-2b7f4a1c8e30",
        "incident_id": "INC-2041",
        "expires_in_sec": 900,
    },
)

MFA_SETUP_RESPONSE_EXAMPLE = OpenApiExample(
    "Fresh TOTP enrollment",
    response_only=True,
    value={
        "secret": "JBSWY3DPEHPK3PXP",
        "otpauth_uri": "otpauth://totp/Morghdari:support%40morghdari.com?secret=JBSWY3DPEHPK3PXP&issuer=Morghdari",
        "confirmed": False,
    },
)
