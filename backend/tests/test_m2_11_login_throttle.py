"""M2-11 acceptance — login brute-force gate (BUG-5, QA 1.26).

WHY: POST /api/v1/auth/login/ had NO DRF throttle — unlimited bad-password
bombing on one account. The gate is generous (auth_login 60/min, keyed by the
ATTEMPTED username so the suite's distinct-user bursts never collide) — burst
past it with bad passwords and DRF must answer 429, while a different legit
user still logs in 200.

فارسی: مسیرِ ورود بدونِ نرخ‌گیر بود؛ پس از انفجارِ تلاشِ ناموفق باید ۴۲۹ بدهد،
اما کاربرِ سالمِ دیگر همچنان ۲۰۰ بگیرد (کلید بر اساسِ نامِ کاربریِ تلاش‌شده).
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.tenants.models import Organization

User = get_user_model()


class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.url = reverse("accounts:auth-login")
        self.password = "strong-pass-123"
        org = Organization.objects.create(name="Throttle Farm")
        self.probe = User.objects.create_user(username="throttle-probe", password=self.password)
        OrgMember.objects.create(user=self.probe, org=org, role="worker", is_active=True)
        self.other = User.objects.create_user(username="throttle-clean", password=self.password)
        OrgMember.objects.create(user=self.other, org=org, role="worker", is_active=True)

    def test_burst_bad_logins_429_but_other_user_still_200(self):
        got_429 = False
        # 60/min lane: 65 bad attempts on ONE username must trip it.
        for _ in range(65):
            res = self.client.post(
                self.url,
                {"username": "throttle-probe", "password": "wrong-pass"},
                format="json",
            )
            if res.status_code == 429:
                got_429 = True
                break
            self.assertIn(res.status_code, (401, 429))
        self.assertTrue(got_429, "expected DRF 429 after a bad-login burst (BUG-5 gate missing?)")
        # A different username rides a different bucket — legit login stays 200.
        ok = self.client.post(
            self.url,
            {"username": "throttle-clean", "password": self.password},
            format="json",
        )
        self.assertEqual(ok.status_code, 200)
