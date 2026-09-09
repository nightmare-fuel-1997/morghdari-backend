"""M7-15 acceptance — the §4s reports base (v1.5 endpoints, real path).

Four surfaces must hold on the REAL middleware path (Bearer → claims → GUC
on Postgres; sqlite laptop loops skip — CI/dev-container truth):

1. FLAGGED BRANCH (D24-8): a report WITH medication lines is created in one
   atomic step — status FLAGGED, report_medication_usages rows written, the
   auto support_tickets row (category=medical, linked_report_id) assigned to
   the org's first ACTIVE doctor, and the `report.flagged` WS event pushed to
   the org group. No medications → plain DRAFT, no ticket, no push.
2. STATE MACHINE: DRAFT → SUBMITTED via PATCH; vet-review moves
   SUBMITTED/FLAGGED → APPROVED/REJECTED with reviewed_by/at + note;
   APPROVED is frozen for tenant editors; reviewer is read-only (403 on
   write/review); engineer is 403 on vet-review.
3. TIME-LOCK (D24-3): a tenant role cannot PATCH a past-day report (403,
   window closed) but CAN patch today's; a §4.5 staff session bypasses the
   window and every bypass writes the REPORT_ADMIN_EDIT audit row.
4. RLS NO-LEAK: foreign saloon/report selectors are 403/404 — never another
   org's rows.

فارسی: پذیرش M7-15 — شاخهٔ FLAGGED، ماشین وضعیت، قفل زمانیِ ویرایش و عدمِ
نشتِ RLS روی مسیر واقعی میان‌افزار؛ فقط روی Postgres اجرا می‌شود.
"""

import unittest
import uuid
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pyotp
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.auditing.models import AccountActivityLog
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.reports.models import (
    DailyReport,
    ReportMedicationUsage,
    SupportTicket,
    VetMedicationCatalog,
)
from apps.telemetry.models import FlockCycle
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"
TEHRAN = ZoneInfo("Asia/Tehran")

# M2-07's exact combo: real migrations (the reports tables + FORCE-RLS only
# exist through migrations 0001) on the real middleware path, PG-only.
pytestmark = [pytest.mark.use_migrations, pytest.mark.django_db(transaction=True)]


def _make_staff(username: str) -> User:
    """Platform staff WITH a confirmed MFA row — the only staff that logs in
    (M7-07 gate) and therefore the only kind that can open a §4.5 session."""
    from apps.accounts import mfa as mfa_mod

    user = User.objects.create_user(username=username, password=PASSWORD)
    user.is_platform_staff = True
    user.save(update_fields=["is_platform_staff"])
    row = mfa_mod.provision(user)
    assert mfa_mod.confirm(user, pyotp.TOTP(row.secret).now())
    return user


def _login(client: APIClient, username: str, mfa_code=None) -> str:
    body = {"username": username, "password": PASSWORD}
    if mfa_code is not None:
        body["mfa_code"] = mfa_code
    response = client.post(reverse("accounts:auth-login"), body, format="json")
    assert response.status_code == 200, response.data
    token = response.data["access_token"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return token


def _session_token(client: APIClient, staff: User, org_id: str) -> str:
    """A real §4.5 support-session token (the audited exemption path)."""
    _login(client, staff.username, mfa_code=pyotp.TOTP(staff.platform_mfa.secret).now())
    opened = client.post(
        reverse("accounts:support-session"),
        {"org_id": org_id, "incident_id": f"INC-{uuid.uuid4().hex[:6]}", "reason": "m7-15 drill"},
        format="json",
    )
    assert opened.status_code == 200, opened.data
    token = opened.data["access_token"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return token


class ReportsBaseTests(TestCase):
    """Common world-builder; PG-only like every real-path module."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("reports truth requires PostgreSQL (CI/dev-container)")

    @staticmethod
    def _make_world(tag: str):
        province = Province.objects.create(name=f"p-{tag}")
        city = City.objects.create(province=province, name=f"c-{tag}")
        org = Organization.objects.create(name=f"org-{tag}")
        other = Organization.objects.create(name=f"other-{tag}")
        farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
        saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
        foreign_farm = Farm.objects.create(org=other, name=f"of-{tag}", province=province, city=city)
        foreign_saloon = Saloon.objects.create(org=other, farm=foreign_farm, name=f"os-{tag}")
        start = datetime.now(TEHRAN) - timedelta(days=5)
        flock = FlockCycle.objects.create(
            org=org, saloon=saloon, breed_type="ross", start_date=start, placement_count=1000
        )
        foreign_flock = FlockCycle.objects.create(
            org=other, saloon=foreign_saloon, breed_type="ross", start_date=start, placement_count=900
        )

        # Staff: engineer writes, doctor(s) receive the FLAGGED ticket,
        # manager reviews, reviewer proves read-only, worker proves
        # read-broad/write-narrow, and a second org's engineer proves RLS.
        engineer = User.objects.create_user(username=f"eng-{tag}", password=PASSWORD)
        doctor = User.objects.create_user(username=f"doc-{tag}", password=PASSWORD)
        doctor2 = User.objects.create_user(username=f"doc2-{tag}", password=PASSWORD)
        manager = User.objects.create_user(username=f"mgr-{tag}", password=PASSWORD)
        reviewer = User.objects.create_user(username=f"rev-{tag}", password=PASSWORD)
        worker = User.objects.create_user(username=f"wrk-{tag}", password=PASSWORD)
        foreign_engineer = User.objects.create_user(username=f"feng-{tag}", password=PASSWORD)
        for user, role in (
            (engineer, "engineer"),
            (doctor, "doctor"),
            (doctor2, "doctor"),
            (manager, "manager"),
            (reviewer, "reviewer"),
            (worker, "worker"),
        ):
            OrgMember.objects.create(user=user, org=org, role=role, is_active=True)
        OrgMember.objects.create(user=foreign_engineer, org=other, role="engineer", is_active=True)

        return {
            "org": org, "other": other,
            "farm": farm, "saloon": saloon, "flock": flock,
            "foreign_farm": foreign_farm, "foreign_saloon": foreign_saloon,
            "foreign_flock": foreign_flock,
            "engineer": engineer, "doctor": doctor, "doctor2": doctor2,
            "manager": manager, "reviewer": reviewer, "worker": worker,
            "foreign_engineer": foreign_engineer,
        }


def _push_layer_capture():
    """A MagicMock channel layer bound over the real resolver — the producer
    goes through apps.reports.api's lazy `get_channel_layer` import, so the
    patch point is channels.layers itself."""
    layer = mock.MagicMock()
    patcher = mock.patch("channels.layers.get_channel_layer", return_value=layer)
    return layer, patcher


class ReportCreateTests(ReportsBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = reverse("reports:saloon-reports", args=[self.w["saloon"].id])

    def _payload(self, **over):
        body = {
            "flock_id": self.w["flock"].flock_id,
            "report_date": datetime.now(TEHRAN).date().isoformat(),
            "mortality_count": 3,
            "feed_kg": "120.50",
            "water_l": "300.00",
            "avg_weight_g": "1850.25",
            "notes": "steady day",
        }
        body.update(over)
        return body

    def test_plain_report_is_draft_201(self):
        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "DRAFT")
        self.assertEqual(ReportMedicationUsage.objects.count(), 0)
        self.assertEqual(SupportTicket.objects.count(), 0)
        self.assertIsNone(response.data["submitted_at"])  # stamps on SUBMITTED only

    def test_duplicate_flock_saloon_date_is_400(self):
        self.assertEqual(self.client.post(self.url, self._payload(), format="json").status_code, 201)
        second = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(second.status_code, 400)

    def test_negative_mortality_is_400(self):
        response = self.client.post(self.url, self._payload(mortality_count=-1), format="json")
        self.assertEqual(response.status_code, 400)

    def test_bad_report_date_is_400(self):
        response = self.client.post(self.url, self._payload(report_date="09/09/2026"), format="json")
        self.assertEqual(response.status_code, 400)

    def test_worker_cannot_write_403(self):
        _login(self.client, self.w["worker"].username)
        self.assertEqual(self.client.post(self.url, self._payload(), format="json").status_code, 403)

    def test_reviewer_cannot_write_403(self):
        _login(self.client, self.w["reviewer"].username)
        self.assertEqual(self.client.post(self.url, self._payload(), format="json").status_code, 403)

    def test_foreign_saloon_is_403_never_leaked(self):
        _login(self.client, self.w["foreign_engineer"].username)
        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, 403)
        # And the foreign engineer cannot aim OUR flock at THEIR saloon —
        # the flock must resolve inside the caller's org.
        other_url = reverse("reports:saloon-reports", args=[self.w["foreign_saloon"].id])
        response = self.client.post(
            other_url, self._payload(flock_id=self.w["flock"].flock_id), format="json"
        )
        self.assertEqual(response.status_code, 400)


class FlaggedBranchTests(ReportsBaseTests):
    """D24-8 — medication lines force FLAGGED + ticket + WS, atomically."""

    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = reverse("reports:saloon-reports", args=[self.w["saloon"].id])

    def _payload(self, **over):
        body = {
            "flock_id": self.w["flock"].flock_id,
            "report_date": datetime.now(TEHRAN).date().isoformat(),
            "mortality_count": 3,
        }
        body.update(over)
        return body

    def test_medications_force_flagged_ticket_and_push(self):
        catalog = VetMedicationCatalog.objects.create(
            org=self.w["org"], created_by=self.w["engineer"], name="Enrofloxacin", unit="mg"
        )
        meds = [
            {"catalog_item_id": catalog.id, "medication_name": "Enrofloxacin", "dosage": "10 mg/kg", "frequency": "24h"},
            {"medication_name": "Vitamin AD3E"},  # no catalog row — the name carries
        ]
        layer, patcher = _push_layer_capture()
        with patcher:
            response = self.client.post(self.url, {**self._payload(), "medications": meds}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "FLAGGED")

        rows = ReportMedicationUsage.objects.order_by("id")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.first().catalog_item_id, catalog.id)
        self.assertEqual(rows.last().medication_name, "Vitamin AD3E")

        # Auto ticket: medical, linked, assigned to the FIRST active doctor
        # (creation order), raised_by the reporter.
        ticket = SupportTicket.objects.get()
        self.assertEqual(ticket.category, "medical")
        self.assertEqual(ticket.linked_report_id, response.data["id"])
        self.assertEqual(ticket.assigned_to_id, self.w["doctor"].id)
        self.assertEqual(ticket.raised_by_id, self.w["engineer"].id)
        self.assertEqual(ticket.status, "OPEN")

        # Exactly one WS push, to the org group, with the D24-8 payload.
        layer.group_send.assert_called_once()
        group, event = layer.group_send.call_args[0]
        self.assertEqual(group, f"reports_{self.w['org'].org_id}")
        self.assertEqual(event["type"], "report.flagged")
        self.assertEqual(event["report_id"], response.data["id"])
        self.assertEqual(event["ticket_id"], ticket.id)
        self.assertEqual(event["medications"][0]["medication_name"], "Enrofloxacin")

    def test_org_without_active_doctor_gets_unassigned_ticket(self):
        OrgMember.objects.filter(org=self.w["org"], role="doctor").update(is_active=False)
        response = self.client.post(
            self.url,
            {**self._payload(), "medications": [{"medication_name": "Amprolium"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        ticket = SupportTicket.objects.get()
        self.assertIsNone(ticket.assigned_to_id)

    def test_no_medications_means_no_push(self):
        layer, patcher = _push_layer_capture()
        with patcher:
            response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "DRAFT")
        layer.group_send.assert_not_called()

    def test_unknown_catalog_item_id_survives_as_name(self):
        response = self.client.post(
            self.url,
            {**self._payload(), "medications": [{"catalog_item_id": 987654, "medication_name": "Ghost Drug"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        row = ReportMedicationUsage.objects.get()
        self.assertIsNone(row.catalog_item_id)
        self.assertEqual(row.medication_name, "Ghost Drug")


class StateMachineTests(ReportsBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        created = self.client.post(
            reverse("reports:saloon-reports", args=[self.w["saloon"].id]),
            {
                "flock_id": self.w["flock"].flock_id,
                "report_date": datetime.now(TEHRAN).date().isoformat(),
                "mortality_count": 2,
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.report_id = created.data["id"]
        self.edit_url = reverse("reports:report-edit", args=[self.report_id])
        self.review_url = reverse("reports:report-vet-review", args=[self.report_id])

    def test_draft_to_submitted_stamps_reporter(self):
        response = self.client.patch(self.edit_url, {"status": "SUBMITTED"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "SUBMITTED")
        self.assertEqual(response.data["submitted_by"], self.w["engineer"].id)
        self.assertIsNotNone(response.data["submitted_at"])

    def test_review_flow_stamps_reviewer(self):
        self.client.patch(self.edit_url, {"status": "SUBMITTED"}, format="json")
        _login(self.client, self.w["manager"].username)
        response = self.client.post(
            self.review_url, {"decision": "APPROVED", "review_note": "ok"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "APPROVED")
        self.assertEqual(response.data["reviewed_by"], self.w["manager"].id)
        self.assertIsNotNone(response.data["reviewed_at"])
        self.assertEqual(response.data["review_note"], "ok")

    def test_reject_flow(self):
        self.client.patch(self.edit_url, {"status": "SUBMITTED"}, format="json")
        _login(self.client, self.w["manager"].username)
        response = self.client.post(
            self.review_url, {"decision": "REJECTED", "review_note": "fix mortality"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "REJECTED")

    def test_flagged_is_reviewable(self):
        # A FLAGGED report (medication path) reaches APPROVED through the
        # same vet-review gate. YESTERDAY's date: setUp's report already
        # holds today's slot for this flock (UNIQUE flock/saloon/date), and
        # vet-review has no time-lock — past-day FLAGGED is reviewable.
        flagged = self.client.post(
            reverse("reports:saloon-reports", args=[self.w["saloon"].id]),
            {
                "flock_id": self.w["flock"].flock_id,
                "report_date": (datetime.now(TEHRAN).date() - timedelta(days=1)).isoformat(),
                "mortality_count": 1,
                "medications": [{"medication_name": "X"}],
            },
            format="json",
        )
        self.assertEqual(flagged.data["status"], "FLAGGED")
        _login(self.client, self.w["manager"].username)
        response = self.client.post(
            reverse("reports:report-vet-review", args=[flagged.data["id"]]),
            {"decision": "APPROVED"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "APPROVED")

    def test_approved_is_frozen_for_tenant_editors(self):
        self.client.patch(self.edit_url, {"status": "SUBMITTED"}, format="json")
        _login(self.client, self.w["manager"].username)
        self.client.post(self.review_url, {"decision": "APPROVED"}, format="json")
        _login(self.client, self.w["engineer"].username)
        response = self.client.patch(self.edit_url, {"mortality_count": 99}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_engineer_cannot_review_403(self):
        self.assertEqual(
            self.client.post(self.review_url, {"decision": "APPROVED"}, format="json").status_code, 403
        )

    def test_reviewer_role_is_read_only_403(self):
        _login(self.client, self.w["reviewer"].username)
        write = self.client.patch(self.edit_url, {"notes": "sneak"}, format="json")
        self.assertEqual(write.status_code, 403)
        review = self.client.post(self.review_url, {"decision": "APPROVED"}, format="json")
        self.assertEqual(review.status_code, 403)

    def test_vet_review_rejects_bad_decision(self):
        # Reviewable-state check precedes body validation (yaml: a report not
        # in SUBMITTED/FLAGGED is 404 regardless of body) — submit first.
        submitted = self.client.patch(self.edit_url, {"status": "SUBMITTED"}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        _login(self.client, self.w["manager"].username)
        response = self.client.post(self.review_url, {"decision": "MAYBE"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_draft_report_is_not_reviewable_404(self):
        _login(self.client, self.w["manager"].username)
        response = self.client.post(self.review_url, {"decision": "APPROVED"}, format="json")
        self.assertEqual(response.status_code, 404)


class TimeLockTests(ReportsBaseTests):
    """D24-3 — same Tehran day only; §4.5 staff session exempt + audited."""

    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        self.today = datetime.now(TEHRAN).date()
        self.past_date = (self.today - timedelta(days=2))
        self.past_report = DailyReport.objects.create(
            org=self.w["org"], farm=self.w["farm"], saloon=self.w["saloon"],
            flock=self.w["flock"], report_date=self.past_date, mortality_count=5, status="DRAFT",
        )
        self.today_report = DailyReport.objects.create(
            org=self.w["org"], farm=self.w["farm"], saloon=self.w["saloon"],
            flock=self.w["flock"], report_date=self.today, mortality_count=1, status="DRAFT",
        )

    def test_tenant_cannot_edit_past_day(self):
        _login(self.client, self.w["engineer"].username)
        response = self.client.patch(
            reverse("reports:report-edit", args=[self.past_report.id]),
            {"mortality_count": 6},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.past_report.refresh_from_db()
        self.assertEqual(self.past_report.mortality_count, 5)

    def test_tenant_can_edit_today(self):
        _login(self.client, self.w["engineer"].username)
        response = self.client.patch(
            reverse("reports:report-edit", args=[self.today_report.id]),
            {"mortality_count": 2},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_staff_session_edits_past_day_and_writes_audit(self):
        staff = _make_staff("supporter")
        _session_token(self.client, staff, str(self.w["org"].org_id))
        response = self.client.patch(
            reverse("reports:report-edit", args=[self.past_report.id]),
            {"mortality_count": 4, "notes": "corrected after incident"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.past_report.refresh_from_db()
        self.assertEqual(self.past_report.mortality_count, 4)
        audit = AccountActivityLog.objects.filter(action="REPORT_ADMIN_EDIT").get()
        self.assertEqual(audit.org_id, self.w["org"].org_id)
        self.assertEqual(audit.actor_id, staff.id)
        self.assertEqual(audit.details["report_id"], self.past_report.id)
        self.assertEqual(audit.details["fields"], ["mortality_count", "notes"])

    def test_forged_session_claim_on_tenant_token_gets_no_exemption(self):
        # The exemption requires platform identity SERVER-SIDE; a tenant user
        # holding a forged support_session token stays time-locked.
        from rest_framework_simplejwt.tokens import AccessToken

        forged = AccessToken.for_user(self.w["engineer"])
        forged["support_session"] = True
        forged["org_id"] = str(self.w["org"].org_id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(forged)}")
        response = self.client.patch(
            reverse("reports:report-edit", args=[self.past_report.id]),
            {"mortality_count": 0},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_foreign_org_report_is_404(self):
        _login(self.client, self.w["foreign_engineer"].username)
        response = self.client.patch(
            reverse("reports:report-edit", args=[self.past_report.id]),
            {"mortality_count": 0},
            format="json",
        )
        self.assertEqual(response.status_code, 404)


class ReportListTests(ReportsBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = reverse("reports:saloon-reports", args=[self.w["saloon"].id])
        today = datetime.now(TEHRAN).date()
        for offset, report_status in ((3, "DRAFT"), (2, "SUBMITTED"), (1, "APPROVED")):
            DailyReport.objects.create(
                org=self.w["org"], farm=self.w["farm"], saloon=self.w["saloon"],
                flock=self.w["flock"], report_date=today - timedelta(days=offset),
                status=report_status,
            )

    def test_list_newest_first_and_filters(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        dates = [row["report_date"] for row in response.data]
        self.assertEqual(dates, sorted(dates, reverse=True))
        today = datetime.now(TEHRAN).date().isoformat()
        filtered = self.client.get(self.url, {"from": today, "to": today})
        self.assertEqual(len(filtered.data), 0)  # every row is past-day
        by_status = self.client.get(self.url, {"status": "APPROVED"})
        self.assertEqual(len(by_status.data), 1)
        self.assertEqual(by_status.data[0]["status"], "APPROVED")

    def test_worker_can_read(self):
        _login(self.client, self.w["worker"].username)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_foreign_saloon_list_is_403(self):
        _login(self.client, self.w["foreign_engineer"].username)
        self.assertEqual(self.client.get(self.url).status_code, 403)
