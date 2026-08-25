"""M2-03 acceptance — endpoints speak the contract, auth class verifies once.

WHY: the OpenAPI contract is CANON: login returns {access_token, refresh_token,
user{...}}, refresh returns native {access} (asymmetry asserted as contractual),
wrong credentials are 401. CachedJWTAuthentication must authenticate a valid
Bearer WITHOUT the middleware present (fallback path) and reject garbage.

فارسی: پذیرش M2-03 — شکل پاسخ‌ها دقیقاً مطابق قرارداد؛ کلاس احراز هویت حتی بدون
میان‌افزار هم کار می‌کند و توکن نامعتبر را رد می‌کند.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient, APIRequestFactory

from apps.accounts.authentication import CachedJWTAuthentication
from apps.accounts.models import OrgMember
from apps.tenants.models import Organization
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


class LoginEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("accounts:auth-login")
        self.password = "strong-pass-123"
        self.user = User.objects.create_user(username="worker1", password=self.password)
        org = Organization.objects.create(name="Golden Egg Farm")
        OrgMember.objects.create(user=self.user, org=org, role="worker", is_active=True)

    def test_login_200_matches_login_response_contract(self):
        response = self.client.post(
            self.url, {"username": "worker1", "password": self.password}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data.keys()), {"access_token", "refresh_token", "user"}
        )
        profile = response.data["user"]
        self.assertEqual(
            set(profile.keys()),
            {"id", "username", "roles", "org_id", "org_name", "is_platform_staff"},
        )
        self.assertEqual(profile["username"], "worker1")
        self.assertEqual(profile["roles"], ["worker"])
        self.assertEqual(profile["org_name"], "Golden Egg Farm")
        self.assertFalse(profile["is_platform_staff"])

    def test_wrong_password_is_401(self):
        response = self.client.post(
            self.url, {"username": "worker1", "password": "nope"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_zero_membership_user_gets_401_not_empty_token(self):
        User.objects.create_user(username="lonely", password=self.password)
        response = self.client.post(
            self.url, {"username": "lonely", "password": self.password}, format="json"
        )
        self.assertEqual(response.status_code, 401)


class RefreshEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("accounts:auth-refresh")

    def test_refresh_returns_native_access_key_only(self):
        user = User.objects.create_user(username="r1", password="strong-pass-123")
        refresh = str(RefreshToken.for_user(user))
        response = self.client.post(self.url, {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, 200)
        # Contractual asymmetry: native `access`, NO access_token/refresh_token.
        self.assertEqual(set(response.data.keys()), {"access"})

    def test_garbage_refresh_is_401(self):
        response = self.client.post(self.url, {"refresh": "not-a-jwt"}, format="json")
        self.assertEqual(response.status_code, 401)


class CachedAuthFallbackTests(TestCase):
    """The auth class works standalone — middleware stash is an optimization."""

    def test_valid_bearer_authenticates_without_middleware_stash(self):
        user = User.objects.create_user(username="a1", password="strong-pass-123")
        access = str(RefreshToken.for_user(user).access_token)
        request = APIRequestFactory().get(
            "/", HTTP_AUTHORIZATION=f"Bearer {access}"
        )
        auth_user, validated = CachedJWTAuthentication().authenticate(request)
        self.assertEqual(auth_user.id, user.id)
        self.assertEqual(validated["user_id"], str(user.id))  # JWT claims are strings

    def test_missing_header_returns_none_then_drf_401s_later(self):
        request = APIRequestFactory().get("/")
        self.assertIsNone(CachedJWTAuthentication().authenticate(request))

    def test_tampered_token_raises_authentication_failed(self):
        user = User.objects.create_user(username="a2", password="strong-pass-123")
        token = str(RefreshToken.for_user(user).access_token)
        request = APIRequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}x")
        with self.assertRaises(AuthenticationFailed):
            CachedJWTAuthentication().authenticate(request)
