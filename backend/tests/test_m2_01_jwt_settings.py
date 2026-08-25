"""M2-01 acceptance — SIMPLE_JWT settings block matches its documentation.

WHY: Security Architecture §3.2 fixes the lifetimes (access 15 min / refresh
7 days); Tracker §4i records the owner's FOREVER decision that rotation and
blacklist stay OFF; constraint C-14 makes deactivation a claims/RLS concern,
so the token_blacklist app must never appear in INSTALLED_APPS.

فارسی: پذیرش M2-01 — تنظیمات JWT باید دقیقاً با سند امنیت و تصمیم‌های مالک
بخواند: دسترسی ۱۵ دقیقه، نوسازی ۷ روز، بدون چرخش و بدون سیاهه؛ اپ
token_blacklist نباید هیچ‌جا ثبت شده باشد.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class SimpleJwtSettingsTests(TestCase):
    def test_token_blacklist_app_is_never_installed(self):
        """Blacklist app absent — its tables would imply revocation we banned."""
        self.assertNotIn(
            "rest_framework_simplejwt.token_blacklist",
            settings.INSTALLED_APPS,
        )

    def test_lifetimes_match_security_section_3_2(self):
        self.assertEqual(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"], timedelta(minutes=15))
        self.assertEqual(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"], timedelta(days=7))

    def test_rotation_and_blacklist_flags_are_off(self):
        self.assertIs(settings.SIMPLE_JWT["ROTATE_REFRESH_TOKENS"], False)
        self.assertIs(settings.SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"], False)

    def test_auth_header_type_is_bearer(self):
        self.assertIn("Bearer", settings.SIMPLE_JWT["AUTH_HEADER_TYPES"])

    def test_bare_serializer_issues_a_token_pair(self):
        """The stock pair works before any customization lands (M2-02 baseline)."""
        user_model = get_user_model()
        user_model.objects.create_user(username="m2_01_worker", password="strong-pass-123")
        serializer = TokenObtainPairSerializer(
            data={"username": "m2_01_worker", "password": "strong-pass-123"}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIn("access", serializer.validated_data)
        self.assertIn("refresh", serializer.validated_data)
