"""M2-06 acceptance — one RBAC choke point + scope-narrowing truth table.

WHY: require_role/RequireRole read the token's FROZEN roles claim (worker
never passes a manager gate), and account_role_scopes narrowing must obey the
doc truth table: saloon row = that saloon, farm row = that farm's saloons,
province row = farms in the province, all-NULL row = whole org, ZERO rows =
unnarrowed (forced by contract v1.4.3 invite, which creates no scope rows).

فارسی: پذیرش M2-06 — نقش‌ها فقط از ادعای توکن؛ جدولِ درستیِ محدودسازی دامنه‌ها
دقیقاً مطابق سند، از جمله حالت «بدون ردیف دامنه = کل سازمان».
"""

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from apps.accounts.models import AccountRoleScope, OrgMember
from apps.accounts.permissions import RequireRole, require_role, scoped_farms, scoped_saloons
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.middleware import CLAIMS_ATTR, TOKEN_STASH_ATTR
from apps.tenants.models import Organization
from rest_framework_simplejwt.tokens import AccessToken

User = get_user_model()


class _FakeToken:
    """Mimics DRF's request.auth payload access without a full request cycle."""

    def __init__(self, payload):
        self.payload = payload


def _request_with_roles(roles):
    request = RequestFactory().get("/")
    setattr(request, CLAIMS_ATTR, {"roles": roles})
    return request


class RequireRoleTests(TestCase):
    def setUp(self):
        self.view = lambda request: HttpResponse("ok")

    def test_matching_role_passes(self):
        wrapped = require_role("manager")(self.view)
        self.assertEqual(wrapped(_request_with_roles(["manager"])).status_code, 200)

    def test_worker_at_manager_gate_is_403(self):
        wrapped = require_role("manager")(self.view)
        response = wrapped(_request_with_roles(["worker"]))
        self.assertEqual(response.status_code, 403)

    def test_any_of_several_roles_passes(self):
        decorated = require_role("owner", "doctor")(self.view)
        self.assertEqual(decorated(_request_with_roles(["worker", "doctor"])).status_code, 200)

    def test_missing_roles_claim_denied(self):
        decorated = require_role("worker")(self.view)
        self.assertEqual(decorated(_request_with_roles([])).status_code, 403)

    def test_drf_permission_class_reads_token_payload_when_no_stash(self):
        permission = RequireRole("owner")
        user = User.objects.create_user(username="p1", password="strong-pass-123")
        token = AccessToken.for_user(user)
        token["roles"] = ["owner"]
        request = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
        setattr(request, "_jwt_validated_token", token)  # middleware stash shape
        request.auth = token  # what DRF assigns after CachedJWTAuthentication runs
        # No CLAIMS_ATTR stash → class falls back to the validated payload.
        self.assertTrue(permission.has_permission(request, view=None))


class ScopeNarrowingTests(TestCase):
    def setUp(self):
        self.province_a = Province.objects.create(name="PA")
        self.province_b = Province.objects.create(name="PB")
        self.city_a = City.objects.create(province=self.province_a, name="CA")
        self.city_b = City.objects.create(province=self.province_b, name="CB")
        self.org = Organization.objects.create(name="scope-org")
        self.other = Organization.objects.create(name="other-org")
        self.user = User.objects.create_user(username="scoped", password="strong-pass-123")
        OrgMember.objects.create(user=self.user, org=self.org, role="worker")
        self.farm1 = Farm.objects.create(org=self.org, name="F1", province=self.province_a, city=self.city_a)
        self.farm2 = Farm.objects.create(org=self.org, name="F2", province=self.province_b, city=self.city_b)
        self.saloon11 = Saloon.objects.create(org=self.org, farm=self.farm1, name="S11")
        self.saloon12 = Saloon.objects.create(org=self.org, farm=self.farm1, name="S12")
        self.saloon21 = Saloon.objects.create(org=self.org, farm=self.farm2, name="S21")

    def _names(self, qs):
        assert qs is not None
        return sorted(qs.values_list("name", flat=True))

    def test_zero_scope_rows_means_unnarrowed(self):
        self.assertIsNone(scoped_farms(self.user, self.org))
        self.assertIsNone(scoped_saloons(self.user, self.org))

    def test_all_null_row_means_whole_org(self):
        AccountRoleScope.objects.create(user=self.user, org=self.org, role="worker")
        self.assertEqual(self._names(scoped_farms(self.user, self.org)), ["F1", "F2"])
        self.assertEqual(
            self._names(scoped_saloons(self.user, self.org)), ["S11", "S12", "S21"]
        )

    def test_saloon_row_narrows_to_that_saloon(self):
        AccountRoleScope.objects.create(
            user=self.user, org=self.org, role="worker", farm=self.farm1, saloon=self.saloon12
        )
        self.assertEqual(self._names(scoped_saloons(self.user, self.org)), ["S12"])
        self.assertEqual(self._names(scoped_farms(self.user, self.org)), ["F1"])

    def test_farm_row_narrows_to_its_saloons(self):
        AccountRoleScope.objects.create(user=self.user, org=self.org, role="worker", farm=self.farm2)
        self.assertEqual(self._names(scoped_farms(self.user, self.org)), ["F2"])
        self.assertEqual(self._names(scoped_saloons(self.user, self.org)), ["S21"])

    def test_province_row_narrows_to_province_farms(self):
        AccountRoleScope.objects.create(
            user=self.user, org=self.org, role="installer", province=self.province_a
        )
        self.assertEqual(self._names(scoped_farms(self.user, self.org)), ["F1"])
        self.assertEqual(self._names(scoped_saloons(self.user, self.org)), ["S11", "S12"])

    def test_rows_union_never_widen_outside_their_reach(self):
        AccountRoleScope.objects.create(user=self.user, org=self.org, role="worker", farm=self.farm2)
        AccountRoleScope.objects.create(
            user=self.user,
            org=self.org,
            role="installer",
            province=self.province_b,
        )
        self.assertEqual(self._names(scoped_farms(self.user, self.org)), ["F2"])
        # Other-org rows are invisible even when they would widen:
        AccountRoleScope.objects.create(user=self.user, org=self.other, role="owner")
        self.assertEqual(self._names(scoped_farms(self.user, self.org)), ["F2"])
