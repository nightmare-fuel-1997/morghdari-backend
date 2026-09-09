"""M7-16 acceptance — alias/conflict/checklist/byte-compat matrix (spec §8).

Covers docs/api-reports-merge-v1.md §8 gates on the REAL path (Bearer →
claims → GUC on Postgres; sqlite laptop loops skip the PG-only matrix —
CI/dev-container truth), plus a portable-DDL proof for daily_report_extras:

1. alias-normalization: pure-Flutter body vs canonical body → same stored row
   (different report_date per POST for the (flock,saloon,date) UNIQUE).
2. both-names-conflict → 400 conflicting fields (mortality + one more pair).
3. checklist POST: SUBMITTED incomplete → 400; DRAFT incomplete → 201 (POST strict).
4. PATCH: SUBMITTED target with incomplete checklist → 400; legacy canonical
   PATCH (no extras keys, no existing row) stays 200 (Task 1 scoping, §7).
5. by-date GET: single → 200 single shape (is_completed + reported_by/at);
   absent → 404; multi-flock same saloon+date → 400 multiple-reports (F1 ruling).
6. byte-compat: old v1.5.0 payload replay → v1.5 subset identical (new keys allowed).
7. portable DDL: daily_report_extras writable via ORM on both vendors
   (M7-08 pattern on sqlite, real migration table on PG).

Prior decisions honored: by-date multi→400 (ledger ruling); bare merged list
(no {results,count} envelope — spec wins); managed models as-is; WITH CHECK
asymmetry family-consistent (migration untouched — it applies).

فارسی: پذیرش M7-16 — ماتریسِ نامِ مستعار/تعارض/چک‌لیست/سازگاریِ بایتی روی مسیر واقعی؛
فقط روی Postgres اجرا می‌شود به‌جز آزمونِ قابلِ حملِ جدولِ ضمیمه.
"""

import unittest
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.reports.models import DailyReport, DailyReportExtras
from apps.telemetry.models import FlockCycle
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"
TEHRAN = ZoneInfo("Asia/Tehran")

# M7-15 convention: real migrations + real middleware path, PG truth.
pytestmark = [pytest.mark.use_migrations, pytest.mark.django_db(transaction=True)]


def _login(client: APIClient, username: str) -> None:
    response = client.post(
        reverse("accounts:auth-login"),
        {"username": username, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.data
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}")


@pytest.fixture(autouse=True)
def _extras_portable_table():
    """M7-08 pattern for daily_report_extras on sqlite.

    On PG the table comes from the migration/syncdb (plus CASCADE-flush patch
    for transactional teardown); on sqlite replay a portable shape carrying
    exactly the columns the ORM touches. CHECK constraints live in the real
    migration; tests write valid kinds.
    """
    if connection.vendor == "postgresql":
        try:
            from tests.conftest import _install_cascade_flush

            _install_cascade_flush(connection)
        except Exception:  # noqa: BLE001 — best-effort, managed tables flush anyway
            pass
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS daily_report_extras ("
            "report_id INTEGER PRIMARY KEY, "
            "org_id VARCHAR(32) NOT NULL, "
            "bird_count INTEGER, "
            "avg_temperature_c NUMERIC(5,2), "
            "avg_humidity_pct NUMERIC(5,2), "
            "ventilation_status VARCHAR(20), "
            "health_status VARCHAR(20), "
            "checklist_feed BOOLEAN NOT NULL DEFAULT 0, "
            "checklist_water BOOLEAN NOT NULL DEFAULT 0, "
            "checklist_device BOOLEAN NOT NULL DEFAULT 0, "
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    yield


class MergeBaseTests(TestCase):
    """PG-only world-builder (M7-15 pattern — sqlite skips cleanly)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("reports merge truth requires PostgreSQL (CI/dev-container)")

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
        engineer = User.objects.create_user(username=f"eng-{tag}", password=PASSWORD)
        manager = User.objects.create_user(username=f"mgr-{tag}", password=PASSWORD)
        worker = User.objects.create_user(username=f"wrk-{tag}", password=PASSWORD)
        reviewer = User.objects.create_user(username=f"rev-{tag}", password=PASSWORD)
        foreign_engineer = User.objects.create_user(username=f"feng-{tag}", password=PASSWORD)
        for user, role in (
            (engineer, "engineer"),
            (manager, "manager"),
            (worker, "worker"),
            (reviewer, "reviewer"),
        ):
            OrgMember.objects.create(user=user, org=org, role=role, is_active=True)
        OrgMember.objects.create(user=foreign_engineer, org=other, role="engineer", is_active=True)
        return {
            "org": org, "other": other,
            "farm": farm, "saloon": saloon, "flock": flock,
            "foreign_saloon": foreign_saloon, "foreign_flock": foreign_flock,
            "engineer": engineer, "manager": manager, "worker": worker,
            "reviewer": reviewer, "foreign_engineer": foreign_engineer,
        }


class AliasNormalizationTests(MergeBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = reverse("reports:saloon-reports", args=[self.w["saloon"].id])

    def test_pure_flutter_body_equals_canonical_body(self):
        """§8 alias-normalization: Flutter aliases + is_draft + Persian
        literals + checklist vs canonical names → same stored row."""
        today = datetime.now(TEHRAN).date()
        flutter_date = today.isoformat()
        canonical_date = (today - timedelta(days=1)).isoformat()
        common_extras = {
            "bird_count": 950,
            "avg_temperature_c": "25.50",
            "avg_humidity_pct": 60,
        }
        flutter_body = {
            "flock_id": self.w["flock"].flock_id,
            "report_date": flutter_date,
            "casualties_today": 3,
            "feed_consumed_kg": "120.50",
            "water_consumed_l": "300.00",
            "avg_sample_weight_g": "1850.25",
            "is_draft": True,
            "ventilation_status": "پایدار",
            "health_status": "عادی",
            "checklist": {"feed": True, "water": True, "device": True},
            "notes": "flutter day",
            **common_extras,
        }
        canonical_body = {
            "flock_id": self.w["flock"].flock_id,
            "report_date": canonical_date,
            "mortality_count": 3,
            "feed_kg": "120.50",
            "water_l": "300.00",
            "avg_weight_g": "1850.25",
            "status": "DRAFT",
            "ventilation_status": "STABLE",
            "health_status": "NORMAL",
            "checklist": {"feed": True, "water": True, "device": True},
            "notes": "flutter day",
            **common_extras,
        }
        flutter_res = self.client.post(self.url, flutter_body, format="json")
        assert flutter_res.status_code == 201, flutter_res.data
        canon_res = self.client.post(self.url, canonical_body, format="json")
        assert canon_res.status_code == 201, canon_res.data

        f, c = flutter_res.data, canon_res.data
        # Canonical output names win on both.
        assert f["mortality_count"] == c["mortality_count"] == 3
        assert str(f["feed_kg"]) == str(c["feed_kg"]) == "120.50"
        assert str(f["water_l"]) == str(c["water_l"]) == "300.00"
        assert str(f["avg_weight_g"]) == str(c["avg_weight_g"]) == "1850.25"
        assert f["status"] == c["status"] == "DRAFT"
        assert f["bird_count"] == c["bird_count"] == 950
        assert str(f["avg_temperature_c"]) == str(c["avg_temperature_c"]) == "25.50"
        assert str(f["avg_humidity_pct"]) == str(c["avg_humidity_pct"]) == "60"
        assert f["ventilation_status"] == c["ventilation_status"] == "STABLE"
        assert f["health_status"] == c["health_status"] == "NORMAL"
        assert f["checklist"] == c["checklist"] == {"feed": True, "water": True, "device": True}
        assert f["is_completed"] is False and c["is_completed"] is False
        assert f["notes"] == c["notes"] == "flutter day"
        # DRAFT: reporter set, stamps empty on both.
        assert f["reported_by"] == c["reported_by"] == self.w["engineer"].id
        assert f["reported_at"] is None and c["reported_at"] is None
        # Dates differ (UNIQUE), everything compared above is equal.
        assert f["report_date"] == flutter_date
        assert c["report_date"] == canonical_date


class ConflictTests(MergeBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = reverse("reports:saloon-reports", args=[self.w["saloon"].id])

    def _base(self, **over):
        body = {
            "flock_id": self.w["flock"].flock_id,
            "report_date": datetime.now(TEHRAN).date().isoformat(),
            "checklist": {"feed": True, "water": True, "device": True},
        }
        body.update(over)
        return body

    def test_both_names_conflict_400(self):
        """§8 both-name conflict → 400 (mortality + at least one more pair)."""
        r1 = self.client.post(
            self.url, self._base(mortality_count=3, casualties_today=5), format="json"
        )
        assert r1.status_code == 400, r1.data
        assert "conflicting fields" in r1.data["error"]
        assert "casualties_today" in r1.data["error"]

        r2 = self.client.post(
            self.url, self._base(feed_kg="10.00", feed_consumed_kg="20.00"), format="json"
        )
        assert r2.status_code == 400, r2.data
        assert "conflicting fields" in r2.data["error"]

        r3 = self.client.post(
            self.url, self._base(status="DRAFT", is_draft=False), format="json"
        )
        assert r3.status_code == 400, r3.data
        assert "conflicting fields" in r3.data["error"]
        assert "is_draft" in r3.data["error"]


class ChecklistPostTests(MergeBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = reverse("reports:saloon-reports", args=[self.w["saloon"].id])

    def test_submitted_incomplete_400_and_draft_incomplete_201(self):
        """§8/§4: SUBMITTED with incomplete checklist → 400 (POST strict);
        DRAFT with incomplete checklist → 201."""
        today = datetime.now(TEHRAN).date()
        bad = self.client.post(
            self.url,
            {
                "flock_id": self.w["flock"].flock_id,
                "report_date": today.isoformat(),
                "mortality_count": 1,
                "status": "SUBMITTED",
                "checklist": {"feed": True, "water": False, "device": True},
            },
            format="json",
        )
        assert bad.status_code == 400, bad.data
        assert "checklist incomplete" in bad.data["error"]

        # is_draft:False alias path is equally strict (no checklist at all).
        bad2 = self.client.post(
            self.url,
            {
                "flock_id": self.w["flock"].flock_id,
                "report_date": (today - timedelta(days=2)).isoformat(),
                "mortality_count": 1,
                "is_draft": False,
            },
            format="json",
        )
        assert bad2.status_code == 400, bad2.data
        assert "checklist incomplete" in bad2.data["error"]

        good = self.client.post(
            self.url,
            {
                "flock_id": self.w["flock"].flock_id,
                "report_date": (today - timedelta(days=1)).isoformat(),
                "mortality_count": 1,
                "status": "DRAFT",
                "checklist": {"feed": True, "water": False, "device": True},
            },
            format="json",
        )
        assert good.status_code == 201, good.data
        assert good.data["status"] == "DRAFT"
        assert good.data["checklist"] == {"feed": True, "water": False, "device": True}


class PatchChecklistTests(MergeBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = reverse("reports:saloon-reports", args=[self.w["saloon"].id])

    def test_patch_submitted_with_incomplete_checklist_400(self):
        today = datetime.now(TEHRAN).date().isoformat()
        created = self.client.post(
            self.url,
            {
                "flock_id": self.w["flock"].flock_id,
                "report_date": today,
                "mortality_count": 2,
                "status": "DRAFT",
                "checklist": {"feed": True, "water": True, "device": False},
            },
            format="json",
        )
        assert created.status_code == 201, created.data
        edit_url = reverse("reports:report-edit", args=[created.data["id"]])
        patched = self.client.patch(edit_url, {"status": "SUBMITTED"}, format="json")
        assert patched.status_code == 400, patched.data
        assert "checklist incomplete" in patched.data["error"]

    def test_patch_legacy_canonical_stays_green(self):
        """Task 1 PATCH scoping: pure-legacy canonical PATCH (no extras keys,
        no existing extras row) stays ungated → 200 per §7 byte-compat."""
        today = datetime.now(TEHRAN).date().isoformat()
        created = self.client.post(
            self.url,
            {
                "flock_id": self.w["flock"].flock_id,
                "report_date": today,
                "mortality_count": 2,
            },
            format="json",
        )
        assert created.status_code == 201, created.data
        with self.assertRaises(DailyReportExtras.DoesNotExist):
            DailyReport.objects.get(pk=created.data["id"]).extras
        edit_url = reverse("reports:report-edit", args=[created.data["id"]])
        patched = self.client.patch(edit_url, {"status": "SUBMITTED"}, format="json")
        assert patched.status_code == 200, patched.data
        assert patched.data["status"] == "SUBMITTED"


class ByDateTests(MergeBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = reverse("reports:saloon-reports", args=[self.w["saloon"].id])

    def _by_date_url(self, day: str) -> str:
        return reverse("reports:saloon-report-by-date", args=[self.w["saloon"].id, day])

    def test_single_200_with_completed_and_reporter(self):
        today = datetime.now(TEHRAN).date().isoformat()
        created = self.client.post(
            self.url,
            {
                "flock_id": self.w["flock"].flock_id,
                "report_date": today,
                "mortality_count": 4,
                "status": "SUBMITTED",
                "bird_count": 900,
                "ventilation_status": "STABLE",
                "health_status": "NORMAL",
                "checklist": {"feed": True, "water": True, "device": True},
            },
            format="json",
        )
        assert created.status_code == 201, created.data
        got = self.client.get(self._by_date_url(today))
        assert got.status_code == 200, got.data
        assert got.data["report_date"] == today
        assert got.data["mortality_count"] == 4
        assert got.data["bird_count"] == 900
        assert got.data["checklist"] == {"feed": True, "water": True, "device": True}
        assert got.data["is_completed"] is False
        assert got.data["reported_by"] == self.w["engineer"].id
        assert got.data["reported_at"] is not None
        # APPROVED flips the calendar flag.
        _login(self.client, self.w["manager"].username)
        reviewed = self.client.post(
            reverse("reports:report-vet-review", args=[created.data["id"]]),
            {"decision": "APPROVED"},
            format="json",
        )
        assert reviewed.status_code == 200, reviewed.data
        _login(self.client, self.w["engineer"].username)
        done = self.client.get(self._by_date_url(today))
        assert done.status_code == 200, done.data
        assert done.data["is_completed"] is True

    def test_absent_404(self):
        got = self.client.get(self._by_date_url("2020-01-01"))
        assert got.status_code == 404, got.data

    def test_multi_flock_same_saloon_date_400(self):
        """F1 ledger ruling: UNIQUE is (flock,saloon,date) so one saloon+day
        can hold N reports — by-date GET with 2+ → 400 multiple-reports."""
        day = datetime.now(TEHRAN).date()
        start = datetime.now(TEHRAN) - timedelta(days=5)
        flock2 = FlockCycle.objects.create(
            org=self.w["org"], saloon=self.w["saloon"],
            breed_type="ross", start_date=start, placement_count=500,
        )
        DailyReport.objects.create(
            org=self.w["org"], farm=self.w["farm"], saloon=self.w["saloon"],
            flock=self.w["flock"], report_date=day, mortality_count=1, status="DRAFT",
        )
        DailyReport.objects.create(
            org=self.w["org"], farm=self.w["farm"], saloon=self.w["saloon"],
            flock=flock2, report_date=day, mortality_count=2, status="DRAFT",
        )
        got = self.client.get(self._by_date_url(day.isoformat()))
        assert got.status_code == 400, got.data
        assert "multiple reports for date" in got.data["error"]


class ByteCompatTests(MergeBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = reverse("reports:saloon-reports", args=[self.w["saloon"].id])

    def test_old_v15_payload_replay_subset_identical(self):
        """§7 byte-compat: old v1.5.0 payload replay → every v1.5 key equal,
        new keys appended only (bare list, no envelope — spec wins)."""
        today = datetime.now(TEHRAN).date().isoformat()
        payload = {
            "flock_id": self.w["flock"].flock_id,
            "report_date": today,
            "mortality_count": 3,
            "feed_kg": "120.50",
            "water_l": "300.00",
            "avg_weight_g": "1850.25",
            "notes": "steady day",
        }
        res = self.client.post(self.url, payload, format="json")
        assert res.status_code == 201, res.data
        data = res.data
        # Every sent v1.5 field round-trips.
        assert data["flock_id"] == payload["flock_id"]
        assert data["report_date"] == today
        assert data["mortality_count"] == 3
        assert str(data["feed_kg"]) == "120.50"
        assert str(data["water_l"]) == "300.00"
        assert str(data["avg_weight_g"]) == "1850.25"
        assert data["notes"] == "steady day"
        assert data["status"] == "DRAFT"
        # The full v1.5 single shape is present.
        for key in (
            "id", "farm_id", "saloon_id", "flock_id", "report_date",
            "mortality_count", "feed_kg", "water_l", "avg_weight_g", "notes",
            "status", "submitted_by", "submitted_at",
            "reviewed_by", "reviewed_at", "review_note",
            "vet_approved_by", "vet_approved_at", "medications",
        ):
            assert key in data, key
        assert data["medications"] == []
        # New §3 keys append with canonical-only defaults (zero overhead path).
        for key in (
            "bird_count", "avg_temperature_c", "avg_humidity_pct",
            "ventilation_status", "health_status", "checklist",
            "is_completed", "reported_by", "reported_at",
        ):
            assert key in data, key
        assert data["bird_count"] is None
        assert data["ventilation_status"] is None
        assert data["health_status"] is None
        assert data["checklist"] == {"feed": False, "water": False, "device": False}
        assert data["is_completed"] is False
        assert data["reported_by"] == data["submitted_by"]
        assert data["reported_at"] == data["submitted_at"]
        # List stays a bare list (no envelope) with the same v1.5 keys.
        listed = self.client.get(self.url)
        assert listed.status_code == 200
        assert isinstance(listed.data, list)
        row = next(r for r in listed.data if r["id"] == data["id"])
        assert row["mortality_count"] == 3
        assert str(row["feed_kg"]) == "120.50"
        assert row["status"] == "DRAFT"
        assert row["checklist"] == {"feed": False, "water": False, "device": False}
        assert row["is_completed"] is False


@pytest.mark.django_db(transaction=True)
def test_extras_side_table_roundtrip_portable():
    """M7-08 portable proof: the side table is writable via ORM on both
    vendors (sqlite uses the portable DDL above; PG uses the real table)."""
    tag = "px" + uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"p-{tag}")
    city = City.objects.create(province=province, name=f"c-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
    start = datetime.now(TEHRAN) - timedelta(days=5)
    flock = FlockCycle.objects.create(
        org=org, saloon=saloon, breed_type="ross", start_date=start, placement_count=100
    )
    report = DailyReport.objects.create(
        org=org, farm=farm, saloon=saloon, flock=flock,
        report_date=datetime.now(TEHRAN).date() - timedelta(days=3),
        mortality_count=1, status="DRAFT",
    )
    DailyReportExtras.objects.update_or_create(
        report=report,
        defaults={
            "org_id": org.org_id, "bird_count": 10,
            "ventilation_status": "STABLE", "health_status": "NORMAL",
            "checklist_feed": True, "checklist_water": True, "checklist_device": True,
        },
    )
    back = DailyReportExtras.objects.get(report=report)
    assert back.bird_count == 10
    assert back.checklist_feed is True
