"""M2-02 acceptance — claim semantics match Security §4.1b exactly.

WHY: claims are frozen at token birth and every later layer (middleware GUC,
require_role, support sessions) trusts them blindly, so the serializer is
pinned by tests: deterministic oldest-active-org pick, sorted role union,
staff flag only when true, and hard 401 for zero active memberships.

فارسی: پذیرش M2-02 — ادعاهای توکن باید دقیقاً مطابق سند امنیت باشند: قدیمی‌ترین
عضویت فعال، اجتماعِ مرتب‌شدهٔ نقش‌ها، پرچم پلتفرم فقط وقتی صحیح، و بدون عضویت فعال
توکنی صادر نشود.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts.models import AccountRoleScope, OrgMember
from apps.accounts.serializers import LoginSerializer
from apps.tenants.models import Organization
from rest_framework_simplejwt.tokens import AccessToken

User = get_user_model()


def make_org(name: str) -> Organization:
    return Organization.objects.create(name=name)


def make_member(user: User, org: Organization, role: str, joined_at=None, is_active=True):
    return OrgMember.objects.create(
        user=user,
        org=org,
        role=role,
        is_active=is_active,
        joined_at=joined_at,
    )


class LoginClaimTests(TestCase):
    def setUp(self):
        self.password = "strong-pass-123"
        self.user = User.objects.create_user(username="worker1", password=self.password)

    def login_claims(self) -> dict:
        serializer = LoginSerializer(data={"username": "worker1", "password": self.password})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        # validate() returns the access token as a STRING (wire format); decode
        # it the same way any client/middleware would to inspect the payload.
        return AccessToken(serializer.validated_data["access"]).payload

    def test_claims_carry_org_id_and_base_role(self):
        org = make_org("farm-a")
        make_member(self.user, org, "worker")
        claims = self.login_claims()
        self.assertEqual(claims["org_id"], str(org.org_id))
        self.assertEqual(claims["roles"], ["worker"])
        # The staff flag is OMITTED when False — platform axis stays silent.
        self.assertNotIn("is_platform_staff", claims)

    def test_roles_are_sorted_union_of_member_and_scopes(self):
        org = make_org("farm-b")
        make_member(self.user, org, "doctor")
        AccountRoleScope.objects.create(user=self.user, org=org, role="owner")
        AccountRoleScope.objects.create(user=self.user, org=org, role="worker")
        claims = self.login_claims()
        # Sorted set-union: base role + scoped roles, duplicates collapsed.
        self.assertEqual(claims["roles"], ["doctor", "owner", "worker"])

    def test_scopes_of_other_orgs_are_ignored(self):
        org_a = make_org("farm-a")
        other = make_org("farm-other")
        make_member(self.user, org_a, "worker")
        AccountRoleScope.objects.create(user=self.user, org=other, role="owner")
        claims = self.login_claims()
        self.assertEqual(claims["roles"], ["worker"])

    def test_multi_org_user_gets_oldest_active_membership(self):
        # Real org_members rows ALWAYS carry joined_at (DB default NOW()); the
        # fixture mirrors that instead of leaving NULLs in the ordering column.
        now = timezone.now()
        older = make_org("farm-old")
        newer = make_org("farm-new")
        make_member(self.user, newer, "manager", joined_at=now)
        make_member(self.user, older, "worker", joined_at=now - timedelta(days=30))
        claims = self.login_claims()
        self.assertEqual(claims["org_id"], str(older.org_id))

    def test_tie_on_joined_at_falls_back_to_lowest_id(self):
        first = make_org("org-first")
        second = make_org("org-second")
        moment = timezone.now()
        member_first = make_member(self.user, first, "worker", joined_at=moment)
        member_second = make_member(self.user, second, "manager", joined_at=moment)
        claims = self.login_claims()
        self.assertEqual(claims["org_id"], str(first.org_id))  # lower id wins

    def test_deactivated_membership_is_invisible_to_login(self):
        org = make_org("farm-a")
        make_member(self.user, org, "worker", is_active=False)
        with self.assertRaises(AuthenticationFailed):
            self.login_claims()

    def test_zero_memberships_rejects_login_with_401_class(self):
        with self.assertRaises(AuthenticationFailed):  # DRF maps this to HTTP 401
            self.login_claims()

    def test_platform_staff_flag_travels_when_true(self):
        org = make_org("farm-a")
        make_member(self.user, org, "worker")
        User.objects.filter(pk=self.user.pk).update(is_platform_staff=True)
        claims = self.login_claims()
        self.assertIs(claims.get("is_platform_staff"), True)
