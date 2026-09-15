"""M7-07 acceptance — support-role activation (Security Architecture §4.5).

Four surfaces must hold on the REAL middleware path:
1. LOGIN GATE: platform-staff login without a live TOTP code is 403
   mfa_required; with the right code it is 200. Tenant users are never asked.
2. ENROLLMENT: setup → confirm is the only path to a confirmed row; a wrong
   confirm code leaves login blocked (two-step — a typo'd secret can't lock
   the on-call engineer out because setup is re-runnable).
3. OPS GATE: the 3 whitelisted ops REQUIRE an ACTIVE support session (claims
   support_session + org_id); staff token without a session, tenant token,
   and superuser all get 403 — one deny for everyone, no existence oracle.
4. OPS SEMANTICS through the REAL services: gateway suspend flips status and
   writes SUPPORT_OP_* with the session incident_id; force-resolve writes
   resolved_at (idempotent on already-resolved); task dispatch runs the real
   drain/cache machinery. Superuser is rejected even with a forged session
   claim, and a session for org A cannot touch org B's objects.

فارسی: پذیرش M7-07 — فعال‌سازی نقش پشتیبانی: ورود پرسنل بدون کد TOTP بسته است،
ثبت‌نام دومرحله‌ای است، و هر سه عملیاتِ مجاز نیازمند نشستِ فعال و ممیزِ حادثه‌اند.
"""

import pyotp
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from unittest import mock
import unittest
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import OrgMember
from apps.alerts.models import AlertInstance
from apps.auditing.models import AccountActivityLog
from apps.devices.models import GatewaySlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"


def _make_staff(username: str, *, mfa_code: str | None = None, superuser: bool = False) -> User:
    user = User.objects.create_user(username=username, password=PASSWORD)
    user.is_platform_staff = True
    if superuser:
        user.is_superuser = True
    user.save(update_fields=["is_platform_staff", "is_superuser"] if superuser else ["is_platform_staff"])
    if mfa_code is not None:
        from apps.accounts import mfa as mfa_mod

        row = mfa_mod.provision(user)
        assert mfa_mod.confirm(user, pyotp.TOTP(row.secret).now())
    return user


def _make_org(name: str) -> Organization:
    return Organization.objects.create(name=name)


def _login(client: APIClient, username: str, mfa_code: str | None = None):
    body = {"username": username, "password": PASSWORD}
    if mfa_code is not None:
        body["mfa_code"] = mfa_code
    return client.post(reverse("accounts:auth-login"), body, format="json")


class StaffMfaLoginGateTests(TestCase):
    """Surface 1+2 — the login gate and the two-step enrollment."""

    def setUp(self):
        self.client = APIClient()

    def test_staff_login_without_mfa_is_403_mfa_required_unenrolled(self):
        _make_staff("bare-staff")
        response = _login(self.client, "bare-staff")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"], "mfa_required")
        self.assertFalse(response.data["enrolled"])

    def test_staff_login_with_wrong_code_is_403(self):
        from apps.accounts import mfa as mfa_mod

        staff = _make_staff("coded-staff")
        row = mfa_mod.provision(staff)
        row.confirmed_at = row.created_at  # simulate confirmed without the dance
        row.save(update_fields=["confirmed_at"])
        response = _login(self.client, "coded-staff", mfa_code="000000")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.data["enrolled"])

    def test_full_enroll_dance_opens_login(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        staff = _make_staff("dance-staff")
        # Login BEFORE enrollment is blocked.
        self.assertEqual(_login(self.client, "dance-staff").status_code, 403)
        # The practical first-run flow: System Admin creates the account and
        # hands over an initial token; the engineer enrolls with it.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(staff).access_token}")
        setup = self.client.post(reverse("accounts:mfa-setup"))
        self.assertEqual(setup.status_code, 200)
        self.assertFalse(setup.data["confirmed"])
        # A wrong confirm code leaves enrollment unconfirmed.
        bad = self.client.post(reverse("accounts:mfa-confirm"), {"code": "000000"}, format="json")
        self.assertEqual(bad.status_code, 400)
        # The right code confirms...
        code = pyotp.TOTP(setup.data["secret"]).now()
        ok = self.client.post(reverse("accounts:mfa-confirm"), {"code": code}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.data["confirmed"])
        # ...and login opens with that code.
        login = _login(self.client, "dance-staff", mfa_code=pyotp.TOTP(setup.data["secret"]).now())
        self.assertEqual(login.status_code, 200, login.data)
        self.assertTrue(login.data["user"]["is_platform_staff"])

    def test_tenant_user_never_asked_for_mfa(self):
        user = User.objects.create_user(username="tenant1", password=PASSWORD)
        OrgMember.objects.create(
            user=user, org=_make_org("T-ORG"), role="worker", is_active=True
        )
        response = _login(self.client, "tenant1")
        self.assertEqual(response.status_code, 200)

    def test_superuser_is_not_gated(self):
        # System Admin = superuser + is_platform_staff (a bare superuser has no
        # org-less token path — D15 raises on zero memberships, 401 by design).
        # The MFA gate must SKIP the superuser even with the staff flag set.
        User.objects.create_superuser(username="breakglass", password=PASSWORD, email="bg@example.com")
        bg = User.objects.get(username="breakglass")
        bg.is_platform_staff = True
        bg.save(update_fields=["is_platform_staff"])
        response = _login(self.client, "breakglass")
        self.assertEqual(response.status_code, 200, response.data)

    def test_mfa_setup_rejects_non_staff(self):
        user = User.objects.create_user(username="plain1", password=PASSWORD)
        from rest_framework_simplejwt.tokens import RefreshToken

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
        self.assertEqual(self.client.post(reverse("accounts:mfa-setup")).status_code, 403)


class SupportOpsGateTests(TestCase):
    """Surface 3 — who may call the ops at all."""

    def setUp(self):
        self.client = APIClient()
        self.org = _make_org("OPS-ORG")
        self.staff = _make_staff("opstaff", mfa_code="enrolled")

    def _staff_token(self) -> str:
        token = _login(self.client, "opstaff", mfa_code=pyotp.TOTP(self.staff.platform_mfa.secret).now())
        self.assertEqual(token.status_code, 200, token.data)
        return token.data["access_token"]

    def _session_token(self) -> str:
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._staff_token()}")
        opened = self.client.post(
            reverse("accounts:support-session"),
            {"org_id": str(self.org.org_id), "incident_id": "INC-7", "reason": "drill"},
            format="json",
        )
        self.assertEqual(opened.status_code, 200, opened.data)
        return opened.data["access_token"]

    def test_ops_require_active_session(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        url = reverse("accounts:support-task-dispatch")
        # staff token WITHOUT a session → 403
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(RefreshToken.for_user(self.staff).access_token)}")
        self.assertEqual(
            self.client.post(url, {"task": "rebuild_threshold_cache", "reason": "x"}, format="json").status_code,
            403,
        )
        # tenant token → 403 (no support_session claim)
        tenant = User.objects.create_user(username="tenant2", password=PASSWORD)
        OrgMember.objects.create(user=tenant, org=self.org, role="manager", is_active=True)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(RefreshToken.for_user(tenant).access_token)}")
        self.assertEqual(
            self.client.post(url, {"task": "rebuild_threshold_cache", "reason": "x"}, format="json").status_code,
            403,
        )

    def test_superuser_rejected_at_session_and_ops(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        superuser = User.objects.create_superuser(
            username="rooty", password=PASSWORD, email="r@example.com"
        )
        forged = AccessToken.for_user(superuser)
        forged["support_session"] = True
        forged["org_id"] = str(self.org.org_id)
        forged["incident_id"] = "INC-X"
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(forged)}")
        # The session opener itself rejects superusers (v0.2 policy, unchanged).
        self.assertEqual(self.client.post(reverse("accounts:support-session"), {}, format="json").status_code, 403)
        # And the ops gate does not trust a forged claim on a superuser token.
        response = self.client.post(
            reverse("accounts:support-task-dispatch"),
            {"task": "rebuild_threshold_cache", "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class SupportOpsSemanticsTests(TestCase):
    """Surface 4 — the ops do real work through the real services, audited."""

    def setUp(self):
        self.client = APIClient()
        self.org = _make_org("SEM-ORG")
        self.staff = _make_staff("semstaff", mfa_code="enrolled")
        self.slot = GatewaySlot.objects.create(
            slot_id="SEM-GW-01", org=self.org, provisioning_status="PROVISIONED"
        )
        # AlertInstance.farm/.saloon are RESTRICT FKs without null=True —
        # minimal real geography/farm/saloon chain (same as the M2-08 pattern).
        province = Province.objects.create(name="P-SEM")
        city = City.objects.create(province=province, name="C-SEM")
        farm = Farm.objects.create(org=self.org, name="F-SEM", province=province, city=city)
        saloon = Saloon.objects.create(org=self.org, farm=farm, name="S-SEM")
        self.alert = AlertInstance.objects.create(
            org=self.org, farm=farm, saloon=saloon,
            metric_type="temp", current_status="ACTIVE_TIER_3",
        )

    def _stub_acl_transport(self):
        """Suspend/resume touch EMQX over HTTP; the FM-side suites already
        prove that behavior — stub it so broker-less CI tests the gate, the
        state matrix and the audit row (M5-07 pattern)."""
        patch1 = mock.patch("apps.mqtt_auth.services.revoke_gateway_acls", lambda slot: True)
        patch2 = mock.patch(
            "apps.mqtt_auth.services.sync_gateway_acls",
            lambda slot: {"synced": 2, "reachable": True},
        )
        patch1.start()
        patch2.start()
        self.addCleanup(patch1.stop)
        self.addCleanup(patch2.stop)
        return patch1

    def _session(self):
        token = _login(
            self.client, "semstaff",
            mfa_code=pyotp.TOTP(self.staff.platform_mfa.secret).now(),
        )
        self.assertEqual(token.status_code, 200, token.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.data['access_token']}")
        opened = self.client.post(
            reverse("accounts:support-session"),
            {"org_id": str(self.org.org_id), "incident_id": "INC-9"},
            format="json",
        )
        self.assertEqual(opened.status_code, 200, opened.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {opened.data['access_token']}")

    def test_suspend_resume_flips_status_and_audits(self):
        """suspend/resume push ACL updates to EMQX over HTTP — broker-less CI
        runs them with a stubbed transport (the M5-07 pattern); the STATE
        matrix and audit row are the code under test here."""
        monkeypatch = self._stub_acl_transport()
        self._session()
        url = reverse("accounts:support-gateway-provisioning", args=[self.slot.slot_id])
        suspend = self.client.post(url, {"action": "suspend", "reason": "INC-9 fan"}, format="json")
        self.assertEqual(suspend.status_code, 200, suspend.data)
        self.assertEqual(suspend.data["provisioning_status"], "SUSPENDED")
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.provisioning_status, "SUSPENDED")

        resume = self.client.post(url, {"action": "resume"}, format="json")
        self.assertEqual(resume.status_code, 200, resume.data)
        self.assertEqual(resume.data["provisioning_status"], "PROVISIONED")

        rows = AccountActivityLog.objects.filter(
            action="SUPPORT_OP_GATEWAY_PROVISIONING"
        ).order_by("timestamp")
        self.assertEqual(rows.count(), 2)  # one per op (suspend, resume)
        self.assertEqual(rows.first().details["incident_id"], "INC-9")
        self.assertEqual(rows.first().details["gateway_slot_id"], "SEM-GW-01")
        self.assertEqual(rows.first().details["action"], "suspend")
        self.assertEqual(rows.last().details["action"], "resume")

    def test_force_resolve_writes_resolved_at_idempotently(self):
        self._session()
        url = reverse("accounts:support-alert-force-resolve", args=[self.alert.pk])
        first = self.client.post(url, {"reason": "runbook §8 stuck-tier defect"}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertTrue(first.data["resolved"])
        self.alert.refresh_from_db()
        self.assertIsNotNone(self.alert.resolved_at)
        second = self.client.post(url, {"reason": "again"}, format="json")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data.get("idempotent"))

    def test_task_dispatch_runs_real_cache_rebuild_and_audits(self):
        monkeypatch = self._stub_acl_transport()
        sent = {}

        class _StubTask:
            def __init__(self, name):
                self.name = name

            def delay(self, *args):
                sent[self.name] = args
                return True

        # Dispatch shape is the contract: the right task name with the right
        # argument. The task bodies have their own suites (M4/M5).
        patch1 = mock.patch(
            "apps.telemetry.tasks.drain_pending_for_gateway", _StubTask("drain")
        )
        patch2 = mock.patch(
            "apps.alerts.tasks.cache_daily_thresholds", _StubTask("recache")
        )
        patch1.start()
        patch2.start()
        self.addCleanup(patch1.stop)
        self.addCleanup(patch2.stop)
        self._session()
        response = self.client.post(
            reverse("accounts:support-task-dispatch"),
            {"task": "rebuild_threshold_cache", "reason": "INC-9 cache drift"},
            format="json",
        )
        self.assertEqual(response.status_code, 202, response.data)
        self.assertIn("recache", sent)



        row = AccountActivityLog.objects.get(action="SUPPORT_OP_TASK_DISPATCH")
        self.assertEqual(row.details["task"], "rebuild_threshold_cache")
        self.assertEqual(row.details["incident_id"], "INC-9")

        # The drain variant dispatches with the gateway id, after an org check.
        drain = self.client.post(
            reverse("accounts:support-task-dispatch"),
            {"task": "drain_pending_approval", "gateway_slot_id": self.slot.slot_id,
             "reason": "INC-9 partial approval drain"},
            format="json",
        )
        self.assertEqual(drain.status_code, 202, drain.data)
        self.assertEqual(sent["drain"], (self.slot.slot_id,))

    def test_session_org_scopes_every_op(self):
        other_org = _make_org("OTHER-ORG")
        other_slot = GatewaySlot.objects.create(
            slot_id="OTHER-GW-01", org=other_org, provisioning_status="PROVISIONED"
        )
        self._session()  # session for self.org
        response = self.client.post(
            reverse("accounts:support-gateway-provisioning", args=[other_slot.slot_id]),
            {"action": "suspend"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)  # no existence oracle across orgs
        other_slot.refresh_from_db()
        self.assertEqual(other_slot.provisioning_status, "PROVISIONED")

    def test_bad_action_and_missing_gateway_id_map_to_400(self):
        self._session()
        bad = self.client.post(
            reverse("accounts:support-gateway-provisioning", args=[self.slot.slot_id]),
            {"action": "nuke"}, format="json",
        )
        self.assertEqual(bad.status_code, 400)
        missing = self.client.post(
            reverse("accounts:support-task-dispatch"),
            {"task": "drain_pending_approval", "reason": "x"}, format="json",
        )
        self.assertEqual(missing.status_code, 400)


class SupportSessionAppRoleTests(TestCase):
    """M7-10 live-drill regression — the opener under the PROD identity.

    Staff tokens are ORG-LESS by design (D15), so the middleware sets no GUC
    for this caller — and organizations is FORCE-RLS'd. The opener used to do
    a bare lookup, which saw ZERO rows as the app role: every session open
    400'd "unknown org_id" on prod while the suite (DB superuser, BYPASSRLS)
    stayed green. These tests SET ROLE morghdari_app with no GUC — the exact
    live shape — so a bare-lookup regression fails here first.
    """

    def setUp(self):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("support-session truth requires PostgreSQL (CI/dev-container)")
        self.client = APIClient()
        self.org = _make_org("APPROLE-ORG")
        self.staff = _make_staff("approlestaff", mfa_code="enrolled")

    def _as_app_role(self):
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE morghdari_app")
        self.addCleanup(self._reset_role)

    def _reset_role(self):
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")

    def _staff_token(self) -> str:
        token = _login(
            self.client, "approlestaff",
            mfa_code=pyotp.TOTP(self.staff.platform_mfa.secret).now(),
        )
        self.assertEqual(token.status_code, 200, token.data)
        return token.data["access_token"]

    def test_session_opens_as_app_role_without_guc(self):
        """The live shape: app role, no GUC, real org UUID → 200 + the org's
        claims + the SUPPORT_ORG_ENTER audit row."""
        self._as_app_role()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._staff_token()}")
        opened = self.client.post(
            reverse("accounts:support-session"),
            {"org_id": str(self.org.org_id), "incident_id": "INC-RLS", "reason": "drill"},
            format="json",
        )
        self.assertEqual(opened.status_code, 200, opened.data)
        self.assertEqual(opened.data["org_id"], str(self.org.org_id))
        self.assertTrue(
            AccountActivityLog.objects.filter(
                action="SUPPORT_ORG_ENTER", org=self.org
            ).exists()
        )

    def test_session_still_400s_unknown_and_garbage_org(self):
        """The fix scopes reads, it does not open them: a wrong UUID and a
        non-UUID both stay 400 unknown org_id (no existence oracle widened)."""
        self._as_app_role()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._staff_token()}")
        import uuid

        bad = self.client.post(
            reverse("accounts:support-session"),
            {"org_id": str(uuid.uuid4()), "incident_id": "INC-RLS"},
            format="json",
        )
        self.assertEqual(bad.status_code, 400, bad.data)
        garbage = self.client.post(
            reverse("accounts:support-session"),
            {"org_id": "not-a-uuid", "incident_id": "INC-RLS"},
            format="json",
        )
        self.assertEqual(garbage.status_code, 400, garbage.data)


class SupportForceResolveRefireTests(TestCase):
    """BUG-9b full lifecycle: force-resolve must clear the coalesce blob so
    the next breach on the same saloon+metric fires a NEW row (no blackhole).

    Wiring: the firing service takes an explicit redis client while the
    support view resolves `system_redis()` internally, so the test patches
    `apps.alerts.services.sweeps.system_redis` onto the SAME real-Redis
    handle the firing calls use (CI REDIS_URL + /11, the firing-suite
    convention) — otherwise the test would compare two different logical
    DBs instead of the lifecycle.
    فارسی: پس از بستنِ اجباری، نقضِ بعدی باید ردیفِ تازه بسازد."""

    def setUp(self):
        import redis as redis_lib

        from tests.conftest import INTEGRATION_REDIS_URL

        self.client = APIClient()
        self.org = _make_org("REFIRE-ORG")
        self.staff = _make_staff("refirestaff", mfa_code="enrolled")
        province = Province.objects.create(name="P-REFIRE")
        city = City.objects.create(province=province, name="C-REFIRE")
        self.farm = Farm.objects.create(
            org=self.org, name="F-REFIRE", province=province, city=city
        )
        self.saloon = Saloon.objects.create(
            org=self.org, farm=self.farm, name="S-REFIRE"
        )
        self.rc = redis_lib.Redis.from_url(
            INTEGRATION_REDIS_URL, decode_responses=True
        )
        self.rc.flushdb()

    def tearDown(self):
        self.rc.flushdb()

    def _session(self):
        token = _login(
            self.client, "refirestaff",
            mfa_code=pyotp.TOTP(self.staff.platform_mfa.secret).now(),
        )
        self.assertEqual(token.status_code, 200, token.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.data['access_token']}")
        opened = self.client.post(
            reverse("accounts:support-session"),
            {"org_id": str(self.org.org_id), "incident_id": "INC-9b"},
            format="json",
        )
        self.assertEqual(opened.status_code, 200, opened.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {opened.data['access_token']}")

    def test_force_resolve_then_rebreach_fires_new_row(self):
        from apps.alerts.services.firing import fire_or_coalesce

        self._session()
        with mock.patch(
            "apps.alerts.services.sweeps.system_redis", lambda: self.rc
        ):
            first = fire_or_coalesce(
                self.rc, self.org.org_id, self.farm.id,
                self.saloon.id, "temp", 35.0,
            )
            self.assertEqual(first["action"], "fired")
            url = reverse(
                "accounts:support-alert-force-resolve", args=[first["alert_id"]]
            )
            resp = self.client.post(
                url, {"reason": "BUG-9b refire drill INC-9b"}, format="json"
            )
            self.assertEqual(resp.status_code, 200, resp.data)
            second = fire_or_coalesce(
                self.rc, self.org.org_id, self.farm.id,
                self.saloon.id, "temp", 36.5,
            )
            self.assertEqual(second["action"], "fired")
            self.assertNotEqual(second["alert_id"], first["alert_id"])
            self.assertEqual(
                AlertInstance.objects.filter(saloon=self.saloon).count(), 2
            )
