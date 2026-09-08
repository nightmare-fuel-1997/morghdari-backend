"""M2-08 acceptance — audited support sessions, end to end (Security §4.5).

WHY: four policies must hold on the REAL middleware path: staff-not-superuser
only, mandatory incident_id, a 15-min scoped token routed by middleware
branch ① (support beats staff-skip), and a SUPPORT_ORG_ENTER audit row written
before success. The integration proof opens a session for org B and shows the
returned access_token seeing EXACTLY that org's saloons under FORCE RLS.

فارسی: پذیرش M2-08 — سیاست‌های نشست پشتیبانی روی مسیر واقعی اثبات می‌شوند؛ توکن
برگشتی دقیقاً فقط سازمانِ مقصد را می‌بیند و ردیف SUPPORT_ORG_ENTER ثبت شده است.
"""

import unittest

import pyotp
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpResponse
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import OrgMember
from apps.auditing.models import AccountActivityLog
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.middleware import TenantOrgMiddleware
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"


@pytest.fixture(scope="class", autouse=True)
def _bootstrap_rls_artifacts(request, seeded_rls_db):
    """These tests SET ROLE morghdari_app — the roles/grants/policies MUST
    exist. Declared explicitly instead of hoping an alphabetically-earlier
    module bootstrapped them first (fresh --create-db sessions broke that)."""
    yield


def _make_org_with_saloon(name: str):
    province = Province.objects.create(name=f"P-{name}")
    city = City.objects.create(province=province, name=f"C-{name}")
    org = Organization.objects.create(name=name)
    farm = Farm.objects.create(org=org, name=f"F-{name}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"S-{name}")
    return org, saloon


def _enroll_mfa(user) -> None:
    """M7-07: the staff login is MFA-gated — pre-enroll actors so these
    suites test the support-session semantics, not the enrollment dance."""
    from apps.accounts import mfa as mfa_mod

    mfa_row = mfa_mod.provision(user)
    assert mfa_mod.confirm(user, pyotp.TOTP(mfa_row.secret).now())


def _login_token(client: APIClient, username: str) -> str:
    body = {"username": username, "password": PASSWORD}
    row = User.objects.filter(username=username).first()
    if row is not None and mfa_mod_has_mfa(row):
        body["mfa_code"] = pyotp.TOTP(row.platform_mfa.secret).now()
    response = client.post(
        reverse("accounts:auth-login"),
        body,
        format="json",
    )
    assert response.status_code == 200, response.data
    return response.data["access_token"]


def mfa_mod_has_mfa(user) -> bool:
    """True when the user has a confirmed MFA row (login needs the code)."""
    return hasattr(user, "platform_mfa") and user.platform_mfa.confirmed_at is not None


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
class SupportSessionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("support-session truth requires PostgreSQL (CI/dev-container)")

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("accounts:support-session")
        self.org_a, _ = _make_org_with_saloon("A")
        self.org_b, _ = _make_org_with_saloon("B")
        self.staff = User.objects.create_user(username="staffer", password=PASSWORD)
        self.staff.is_platform_staff = True
        self.staff.save(update_fields=["is_platform_staff"])
        _enroll_mfa(self.staff)

    def test_staff_opens_session_and_audit_row_precedes_success(self):
        token = _login_token(self.client, "staffer")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.post(
            self.url,
            {"org_id": str(self.org_b.org_id), "incident_id": "INC-42", "reason": "fan alarm"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data.keys()),
            {"access_token", "org_id", "incident_id", "expires_in_sec"},
        )
        self.assertEqual(response.data["expires_in_sec"], 900)
        self.assertEqual(response.data["org_id"], str(self.org_b.org_id))

        row = AccountActivityLog.objects.get(action="SUPPORT_ORG_ENTER")
        self.assertEqual(row.actor_id, self.staff.id)
        self.assertEqual(row.org_id, self.org_b.org_id)
        self.assertEqual(row.details["incident_id"], "INC-42")
        self.assertEqual(row.details["reason"], "fan alarm")

        # The returned token decodes to exactly the promised scoped claims.
        payload = AccessToken(response.data["access_token"]).payload
        self.assertIs(payload["support_session"], True)
        self.assertEqual(payload["org_id"], str(self.org_b.org_id))
        self.assertEqual(payload["incident_id"], "INC-42")

    def test_superuser_is_rejected_even_with_staff_flag(self):
        self.staff.is_superuser = True
        self.staff.save(update_fields=["is_superuser", "is_platform_staff"])
        token = _login_token(self.client, "staffer")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.post(
            self.url,
            {"org_id": str(self.org_b.org_id), "incident_id": "INC-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_plain_tenant_member_is_rejected(self):
        member = User.objects.create_user(username="plain", password=PASSWORD)
        OrgMember.objects.create(user=member, org=self.org_a, role="worker", is_active=True)
        token = _login_token(self.client, "plain")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.post(
            self.url,
            {"org_id": str(self.org_b.org_id), "incident_id": "INC-2"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_blank_incident_id_is_400(self):
        token = _login_token(self.client, "staffer")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        for body in ({"org_id": str(self.org_a.org_id)}, {"incident_id": "   "}):
            response = self.client.post(self.url, body, format="json")
            self.assertEqual(response.status_code, 400)

    def test_unknown_org_is_400(self):
        token = _login_token(self.client, "staffer")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.post(
            self.url,
            {"org_id": "00000000-0000-0000-0000-000000000000", "incident_id": "INC-3"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_returned_token_sees_only_target_org_via_middleware(self):
        token = _login_token(self.client, "staffer")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        opened = self.client.post(
            self.url,
            {"org_id": str(self.org_b.org_id), "incident_id": "INC-9"},
            format="json",
        )
        scoped = opened.data["access_token"]

        class Probe:
            def __call__(self, request):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_setting('app.current_org_id', true)")
                    self.guc = cursor.fetchone()[0]
                self.saloons_seen = sorted(Saloon.objects.values_list("name", flat=True))
                return HttpResponse("ok")

        probe = Probe()
        request = APIRequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {scoped}")

        with connection.cursor() as cursor:
            cursor.execute("SET ROLE morghdari_app")
        try:
            TenantOrgMiddleware(probe)(request)
            assert probe.guc == str(self.org_b.org_id)
            assert probe.saloons_seen == ["S-B"]  # org A invisible despite staff holder
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
