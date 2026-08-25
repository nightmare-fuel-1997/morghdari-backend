"""M2-07 acceptance — org-member management per contract v1.4.3, end to end.

WHY: these requests run the REAL middleware path (Bearer → claims → GUC on
postgres), so the module is postgres-only like test_m2_05's branch proofs —
sqlite laptop loops skip; integration truth lives in CI/dev-container runs.

Policies proven: owner-exclusive writes (403 others) · invite creates user
with UNUSABLE password (201) · duplicate ACTIVE membership 409 · invite over
INACTIVE reactivates (200) · deactivate soft-off with {is_active:false} ·
unknown/foreign member 404 · deactivated member cannot log in again.

فارسی: پذیرش M2-07 — سیاست‌های قرارداد روی مسیر واقعیِ میان‌افزار و RLS اثبات
می‌شوند؛ فقط روی Postgres اجرا می‌شود.
"""

import unittest

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"


def _login(client: APIClient, username: str) -> str:
    response = client.post(
        reverse("accounts:auth-login"),
        {"username": username, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.data
    return response.data["access_token"]


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
class OrgMembersEndpointTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("member management truth requires PostgreSQL (CI/dev-container)")

    def setUp(self):
        self.client = APIClient()
        self.list_url = reverse("accounts:org-members")
        self.org = Organization.objects.create(name="member-org")
        self.owner = User.objects.create_user(username="owner1", password=PASSWORD)
        self.owner_member = OrgMember.objects.create(
            user=self.owner, org=self.org, role="owner", is_active=True
        )

    def _auth(self, client_or_token):
        token = client_or_token if isinstance(client_or_token, str) else client_or_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_invite_creates_user_with_unusable_password_201(self):
        self._auth(_login(self.client, "owner1"))
        response = self.client.post(
            self.list_url, {"username": "new.hire", "role": "worker"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["is_active"])
        self.assertEqual(response.data["role"], "worker")
        created = User.objects.get(username="new.hire")
        self.assertFalse(created.has_usable_password())

    def test_duplicate_active_membership_is_409(self):
        self._auth(_login(self.client, "owner1"))
        first = self.client.post(
            self.list_url, {"username": "dup", "role": "worker"}, format="json"
        )
        self.assertEqual(first.status_code, 201)
        second = self.client.post(
            self.list_url, {"username": "dup", "role": "manager"}, format="json"
        )
        self.assertEqual(second.status_code, 409)

    def test_invalid_role_is_400(self):
        self._auth(_login(self.client, "owner1"))
        response = self.client.post(
            self.list_url, {"username": "x", "role": "emperor"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_non_owner_write_is_403_but_read_ok(self):
        OrgMember.objects.create(
            user=User.objects.create_user(username="mgr1", password=PASSWORD),
            org=self.org,
            role="manager",
            is_active=True,
        )
        token = _login(self.client, "mgr1")
        self._auth(token)
        listed = self.client.get(self.list_url)
        self.assertEqual(listed.status_code, 200)  # any member may read
        invited = self.client.post(
            self.list_url, {"username": "nope", "role": "worker"}, format="json"
        )
        self.assertEqual(invited.status_code, 403)

    def test_deactivate_then_reactivate_roundtrip(self):
        self._auth(_login(self.client, "owner1"))
        hired = self.client.post(
            self.list_url, {"username": "temp", "role": "worker"}, format="json"
        )
        member_id = hired.data["member_id"]
        off = self.client.post(reverse("accounts:org-member-deactivate", args=[member_id]))
        self.assertEqual(off.status_code, 200)
        self.assertIs(off.data["is_active"], False)
        on_again = self.client.post(
            self.list_url, {"username": "temp", "role": "worker"}, format="json"
        )
        self.assertEqual(on_again.status_code, 200)  # REACTIVATED, same row
        self.assertEqual(on_again.data["member_id"], member_id)

    def test_foreign_member_deactivate_is_404(self):
        other = Organization.objects.create(name="elsewhere")
        stranger = OrgMember.objects.create(
            user=User.objects.create_user(username="stranger", password=PASSWORD),
            org=other,
            role="worker",
            is_active=True,
        )
        self._auth(_login(self.client, "owner1"))
        response = self.client.post(
            reverse("accounts:org-member-deactivate", args=[stranger.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_list_shows_all_flags_including_inactive(self):
        inactive = OrgMember.objects.create(
            user=User.objects.create_user(username="gone", password=PASSWORD),
            org=self.org,
            role="worker",
            is_active=False,
        )
        self._auth(_login(self.client, "owner1"))
        listed = self.client.get(self.list_url)
        ids = {row["member_id"] for row in listed.data}
        self.assertIn(self.owner_member.id, ids)
        self.assertIn(inactive.id, ids)
        gone_row = next(r for r in listed.data if r["member_id"] == inactive.id)
        self.assertIs(gone_row["is_active"], False)

    def test_deactivated_member_cannot_login_again(self):
        leaver = User.objects.create_user(username="leaver", password=PASSWORD)
        OrgMember.objects.create(user=leaver, org=self.org, role="worker", is_active=True)
        # Sanity: while ACTIVE they log in fine.
        active_login = APIClient().post(
            reverse("accounts:auth-login"),
            {"username": "leaver", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(active_login.status_code, 200)

        self._auth(_login(self.client, "owner1"))
        listed = self.client.get(self.list_url)
        member_id = next(
            r["member_id"] for r in listed.data if r["username"] == "leaver"
        )
        off = self.client.post(reverse("accounts:org-member-deactivate", args=[member_id]))
        self.assertEqual(off.status_code, 200)

        # Zero ACTIVE memberships ⇒ AuthenticationFailed ⇒ 401 at login.
        blocked = APIClient().post(
            reverse("accounts:auth-login"),
            {"username": "leaver", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(blocked.status_code, 401)
