"""M2-04 acceptance — no-rotation semantics proven, not just configured.

WHY: rotation and blacklist are OFF forever (owner decision, Tracker §4i). The
consequence must be DEMONSTRATED: one refresh token reused twice yields two
valid access tokens; no blacklist tables can exist because the app was never
installed; a tampered signature is rejected at the refresh endpoint.

فارسی: پذیرش M2-04 — بدون چرخش و سیاهه، یک توکن نوسازی باید دوباره قابل استفاده
باشد؛ اپ token_blacklist نباید نصب باشد؛ امضای دست‌کاری‌شده رد شود.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


class NoRotationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("accounts:auth-refresh")
        self.user = User.objects.create_user(username="r2", password="strong-pass-123")

    def test_same_refresh_token_works_twice(self):
        """The core no-rotation proof: reuse is VALID until natural expiry."""
        refresh = str(RefreshToken.for_user(self.user))
        first = self.client.post(self.url, {"refresh": refresh}, format="json")
        second = self.client.post(self.url, {"refresh": refresh}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        # Both minted a usable access token (different lifetimes ⇒ different JWTs).
        self.assertNotEqual(first.data["access"], second.data["access"])

    def test_blacklist_tables_cannot_exist(self):
        """App never installed ⇒ its models are unreachable in the registry."""
        from django.apps import apps

        with self.assertRaises(LookupError):
            apps.get_app_config("token_blacklist")

    def test_tampered_signature_rejected_at_refresh_endpoint(self):
        token = str(RefreshToken.for_user(self.user))
        tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        response = self.client.post(self.url, {"refresh": tampered}, format="json")
        self.assertEqual(response.status_code, 401)
