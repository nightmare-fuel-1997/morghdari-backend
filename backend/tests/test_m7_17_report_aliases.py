"""M7-17 acceptance — W1–W5 report aliases over the canonical merge surface.

Spec: docs/api-reports-merge-v1.md §5 (W table) + §3 (response shapes);
compat error contract docs/api-compat-flutter-v1.md §3/§8
({"error": ...} envelope, foreign/bad slug → 404, inverted window → 400).

Prior contracts honored: Task 1 by-date multi→400 multiple-reports (rides
along through W2); bare merged list (no {results,count} envelope — spec
wins); PATCH in-play checklist scoping (rides along through W4);
reporter = JWT user, never client input (W3 ignores any client-sent
reporter/reported_by key); slugs via legacy_slug_map
(compat_resolve_farm/saloon — kind 'device' IS the salon).

PG-only world builder (M7-15/M7-16 pattern — sqlite skips cleanly);
use_migrations so the real legacy_slug_map + daily_report_extras tables
exist (plus the to_regclass guard copied from M6-09).

Covered:
- W1 list == canonical list (ids, order, full dicts); from/to/status
  passthrough; ?status=incomplete → DRAFT+SUBMITTED only.
- W1/W2/W5 foreign slug → 404 {"error"}; farm/salon mismatch → 404
  (never a 400 oracle); inverted window → 400.
- W2 single == canonical single; absent → 404; multi-flock day → 400.
- W3 flutter write (aliases + Persian literals + checklist) → 201;
  missing slugs → 400; mismatch/foreign → 404; client reporter ignored;
  checklist + FLAGGED ride along.
- W4 alias PATCH → 200; checklist-incomplete SUBMITTED → 400.
- W5 farm queue spans the farm's saloons, excludes other farms.

فارسی: پذیرش W1–W5 — نازل‌های گزارش روی سطحِ کانونی؛ اسلاگِ خارجی ۴۰۴،
بازهٔ وارونه ۴۰۰، گزارشگر همیشه کاربرِ JWT.
"""

import unittest
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.compat.models import LegacySlugMap
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.reports.models import DailyReport
from apps.telemetry.models import FlockCycle
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"
TEHRAN = ZoneInfo("Asia/Tehran")

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
def _w_compat_tables():
    """PG: cascade-flush patch + legacy_slug_map DDL guard (M6-09 pattern —
    unmanaged tables are invisible to --nomigrations teardown otherwise)."""
    if connection.vendor == "postgresql":
        try:
            from tests.conftest import _install_cascade_flush

            _install_cascade_flush(connection)
        except Exception:  # noqa: BLE001 — best-effort, managed tables flush anyway
            pass
        with connection.cursor() as cursor:
            missing = cursor.execute(
                "SELECT to_regclass('public.legacy_slug_map') IS NULL"
            ).fetchone()[0]
            if missing:
                from tests.conftest import (
                    BACKEND_DIR,
                    _execute_sql,
                    _sql_from_migration,
                )

                _execute_sql(
                    cursor,
                    _sql_from_migration(
                        BACKEND_DIR / "apps" / "compat" / "migrations" / "0001_legacy_slug_map.py"
                    ),
                )
    yield


class AliasBaseTests(TestCase):
    """PG-only world: org with two farms (mismatch probe), two saloons in
    farm 1 (W5 span probe), flocks, engineer+manager, backfilled slugs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("W-alias truth requires PostgreSQL (CI/dev-container)")

    @staticmethod
    def _make_world(tag: str):
        province = Province.objects.create(name=f"p-{tag}")
        city = City.objects.create(province=province, name=f"c-{tag}")
        org = Organization.objects.create(name=f"org-{tag}")
        other = Organization.objects.create(name=f"other-{tag}")
        farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
        farm2 = Farm.objects.create(org=org, name=f"f2-{tag}", province=province, city=city)
        saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
        saloon_b = Saloon.objects.create(org=org, farm=farm, name=f"sb-{tag}")
        saloon_f2 = Saloon.objects.create(org=org, farm=farm2, name=f"sf2-{tag}")
        foreign_farm = Farm.objects.create(org=other, name=f"of-{tag}", province=province, city=city)
        foreign_saloon = Saloon.objects.create(org=other, farm=foreign_farm, name=f"os-{tag}")
        start = datetime.now(TEHRAN) - timedelta(days=5)
        flock = FlockCycle.objects.create(
            org=org, saloon=saloon, breed_type="ross", start_date=start, placement_count=1000
        )
        flock_b = FlockCycle.objects.create(
            org=org, saloon=saloon_b, breed_type="ross", start_date=start, placement_count=800
        )
        foreign_flock = FlockCycle.objects.create(
            org=other, saloon=foreign_saloon, breed_type="ross", start_date=start, placement_count=900
        )
        engineer = User.objects.create_user(username=f"eng-{tag}", password=PASSWORD)
        manager = User.objects.create_user(username=f"mgr-{tag}", password=PASSWORD)
        stranger = User.objects.create_user(username=f"str-{tag}", password=PASSWORD)
        OrgMember.objects.create(user=engineer, org=org, role="engineer", is_active=True)
        OrgMember.objects.create(user=manager, org=org, role="manager", is_active=True)
        OrgMember.objects.create(user=stranger, org=other, role="engineer", is_active=True)

        call_command("backfill_legacy_slugs", verbosity=0)

        def slug(kind, **kw):
            return LegacySlugMap.objects.get(org_id=org.org_id, kind=kind, **kw).slug

        return {
            "org": org, "other": other,
            "farm": farm, "farm2": farm2,
            "saloon": saloon, "saloon_b": saloon_b, "saloon_f2": saloon_f2,
            "flock": flock, "flock_b": flock_b,
            "foreign_saloon": foreign_saloon, "foreign_flock": foreign_flock,
            "engineer": engineer, "manager": manager, "stranger": stranger,
            "slugs": {
                "farm": slug("farm", farm_id=farm.pk),
                "farm2": slug("farm", farm_id=farm2.pk),
                "salon": slug("device", saloon_id=saloon.pk),
                "salon_b": slug("device", saloon_id=saloon_b.pk),
                "salon_f2": slug("device", saloon_id=saloon_f2.pk),
                "farm_foreign": LegacySlugMap.objects.get(
                    org_id=other.org_id, kind="farm", farm_id=foreign_farm.pk
                ).slug,
                "salon_foreign": LegacySlugMap.objects.get(
                    org_id=other.org_id, kind="device", saloon_id=foreign_saloon.pk
                ).slug,
            },
        }

    def _w1(self, w, salon_key="salon", farm_key="farm"):
        return (
            f"/api/compat/v1/farms/{w['slugs'][farm_key]}"
            f"/salons/{w['slugs'][salon_key]}/reports/"
        )

    def _w2(self, w, day, salon_key="salon", farm_key="farm"):
        return self._w1(w, salon_key, farm_key) + f"{day}/"

    def _w5(self, w, farm_key="farm"):
        return f"/api/compat/v1/farms/{w['slugs'][farm_key]}/reports/"

    def _canon_list(self, w, key="saloon"):
        return reverse("reports:saloon-reports", args=[w[key].id])

    def _canon_post(self, client, w, day, **over):
        body = {
            "flock_id": w["flock"].flock_id,
            "report_date": day,
            "mortality_count": 2,
        }
        body.update(over)
        res = client.post(self._canon_list(w), body, format="json")
        assert res.status_code == 201, res.data
        return res


class W1ListTests(AliasBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)

    def test_w1_matches_canonical_list(self):
        """W1 == canonical GET: same bare list, same order, same dicts."""
        today = datetime.now(TEHRAN).date()
        r1 = self._canon_post(self.client, self.w, (today - timedelta(days=2)).isoformat())
        r2 = self._canon_post(
            self.client, self.w, today.isoformat(), bird_count=910,
            checklist={"feed": True, "water": True, "device": True},
        )
        canon = self.client.get(self._canon_list(self.w))
        assert canon.status_code == 200
        got = self.client.get(self._w1(self.w))
        assert got.status_code == 200, got.data
        assert isinstance(got.data, list)  # bare list — spec wins, no envelope
        assert got.data == canon.data
        assert [r["id"] for r in got.data] == [r2.data["id"], r1.data["id"]]  # newest first
        assert got.data[0]["bird_count"] == 910

    def test_w1_from_to_status_passthrough(self):
        today = datetime.now(TEHRAN).date()
        self._canon_post(self.client, self.w, (today - timedelta(days=3)).isoformat())
        mid = self._canon_post(self.client, self.w, (today - timedelta(days=1)).isoformat())
        canon = self.client.get(
            self._canon_list(self.w),
            {"from": (today - timedelta(days=2)).isoformat(),
             "to": today.isoformat(), "status": "DRAFT"},
        )
        got = self.client.get(
            self._w1(self.w),
            {"from": (today - timedelta(days=2)).isoformat(),
             "to": today.isoformat(), "status": "DRAFT"},
        )
        assert got.status_code == 200, got.data
        assert [r["id"] for r in got.data] == [r["id"] for r in canon.data] == [mid.data["id"]]

    def test_w1_incomplete_is_draft_plus_submitted(self):
        """W5 semantics on W1: ?status=incomplete → DRAFT+SUBMITTED only."""
        today = datetime.now(TEHRAN).date()
        full = {"feed": True, "water": True, "device": True}
        draft = self._canon_post(self.client, self.w, today.isoformat(), status="DRAFT")
        sub = self._canon_post(
            self.client, self.w, (today - timedelta(days=1)).isoformat(),
            status="SUBMITTED", checklist=dict(full),
        )
        done = self._canon_post(
            self.client, self.w, (today - timedelta(days=2)).isoformat(),
            status="SUBMITTED", checklist=dict(full),
        )
        _login(self.client, self.w["manager"].username)
        reviewed = self.client.post(
            reverse("reports:report-vet-review", args=[done.data["id"]]),
            {"decision": "APPROVED"}, format="json",
        )
        assert reviewed.status_code == 200, reviewed.data
        _login(self.client, self.w["engineer"].username)
        got = self.client.get(self._w1(self.w), {"status": "incomplete"})
        assert got.status_code == 200, got.data
        assert {r["id"] for r in got.data} == {draft.data["id"], sub.data["id"]}
        assert all(r["status"] in ("DRAFT", "SUBMITTED") for r in got.data)

    def test_w1_foreign_and_mismatched_slugs_404(self):
        """Foreign slug → 404 {"error"}; same-org farm/salon disagreement →
        404 as well — never a 400 oracle, never 500."""
        w = self.w
        foreign_farm_url = (
            f"/api/compat/v1/farms/{w['slugs']['farm_foreign']}"
            f"/salons/{w['slugs']['salon']}/reports/"
        )
        res = self.client.get(foreign_farm_url)
        assert res.status_code == 404, res.data
        assert "error" in res.data
        foreign_salon_url = (
            f"/api/compat/v1/farms/{w['slugs']['farm']}"
            f"/salons/{w['slugs']['salon_foreign']}/reports/"
        )
        res = self.client.get(foreign_salon_url)
        assert res.status_code == 404, res.data
        assert "error" in res.data
        mismatch = self.client.get(self._w1(w, salon_key="salon_f2"))
        assert mismatch.status_code == 404, mismatch.data
        assert "error" in mismatch.data

    def test_w1_inverted_window_400(self):
        today = datetime.now(TEHRAN).date()
        res = self.client.get(
            self._w1(self.w),
            {"from": today.isoformat(), "to": (today - timedelta(days=8)).isoformat()},
        )
        assert res.status_code == 400, res.data
        assert "error" in res.data

    def test_w1_bad_date_400(self):
        res = self.client.get(self._w1(self.w), {"from": "not-a-date"})
        assert res.status_code == 400, res.data
        assert "error" in res.data


class W2ByDateTests(AliasBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)

    def test_w2_matches_canonical_single(self):
        today = datetime.now(TEHRAN).date().isoformat()
        created = self._canon_post(
            self.client, self.w, today, mortality_count=4, bird_count=900,
            status="SUBMITTED",
            checklist={"feed": True, "water": True, "device": True},
        )
        canon = self.client.get(
            reverse("reports:saloon-report-by-date", args=[self.w["saloon"].id, today])
        )
        assert canon.status_code == 200
        got = self.client.get(self._w2(self.w, today))
        assert got.status_code == 200, got.data
        assert got.data == canon.data
        assert got.data["id"] == created.data["id"]
        assert got.data["reported_by"] == self.w["engineer"].id

    def test_w2_absent_404(self):
        got = self.client.get(self._w2(self.w, "2020-01-01"))
        assert got.status_code == 404, got.data
        assert "error" in got.data

    def test_w2_multi_flock_day_400(self):
        """Task 1 F1 ruling rides along: 2 flocks, one saloon+day → 400."""
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
        got = self.client.get(self._w2(self.w, day.isoformat()))
        assert got.status_code == 400, got.data
        assert "multiple reports for date" in got.data["error"]

    def test_w2_foreign_slug_404(self):
        url = (
            f"/api/compat/v1/farms/{self.w['slugs']['farm']}"
            f"/salons/{self.w['slugs']['salon_foreign']}/reports/2026-09-09/"
        )
        got = self.client.get(url)
        assert got.status_code == 404, got.data
        assert "error" in got.data


class W3CreateTests(AliasBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)
        self.url = "/api/compat/v1/reports/"

    def _flutter_body(self, w, day, **over):
        body = {
            "farm_slug": w["slugs"]["farm"],
            "salon_slug": w["slugs"]["salon"],
            "flock_id": w["flock"].flock_id,
            "report_date": day,
            "casualties_today": 1,
            "is_draft": True,
            "ventilation_status": "پایدار",
            "health_status": "عادی",
            "checklist": {"feed": True, "water": True, "device": True},
        }
        body.update(over)
        return body

    def test_w3_flutter_write_resolves_slugs(self):
        today = datetime.now(TEHRAN).date().isoformat()
        res = self.client.post(self.url, self._flutter_body(self.w, today), format="json")
        assert res.status_code == 201, res.data
        assert res.data["mortality_count"] == 1
        assert res.data["status"] == "DRAFT"
        assert res.data["ventilation_status"] == "STABLE"
        assert res.data["health_status"] == "NORMAL"

    def test_w3_missing_slugs_400(self):
        today = datetime.now(TEHRAN).date().isoformat()
        body = self._flutter_body(self.w, today)
        del body["farm_slug"]
        res = self.client.post(self.url, body, format="json")
        assert res.status_code == 400, res.data
        assert "error" in res.data
        body = self._flutter_body(
            self.w, (datetime.now(TEHRAN).date() - timedelta(days=1)).isoformat()
        )
        del body["salon_slug"]
        res = self.client.post(self.url, body, format="json")
        assert res.status_code == 400, res.data

    def test_w3_mismatch_and_foreign_404(self):
        """Disagreement (salon of farm2 under farm1) → 404, never a
        400 oracle; foreign-org slugs → 404 {"error"}."""
        today = datetime.now(TEHRAN).date()
        body = self._flutter_body(self.w, today.isoformat(), salon_slug=self.w["slugs"]["salon_f2"])
        res = self.client.post(self.url, body, format="json")
        assert res.status_code == 404, res.data
        assert "error" in res.data
        body = self._flutter_body(
            self.w, (today - timedelta(days=1)).isoformat(),
            salon_slug=self.w["slugs"]["salon_foreign"],
        )
        res = self.client.post(self.url, body, format="json")
        assert res.status_code == 404, res.data
        assert "error" in res.data
        body = self._flutter_body(
            self.w, (today - timedelta(days=2)).isoformat(),
            farm_slug=self.w["slugs"]["farm_foreign"],
        )
        res = self.client.post(self.url, body, format="json")
        assert res.status_code == 404, res.data

    def test_w3_reporter_is_jwt_user(self):
        """A client-sent reporter/reported_by/submitted_by is ignored —
        reported_by is ALWAYS the JWT engineer."""
        today = datetime.now(TEHRAN).date().isoformat()
        body = self._flutter_body(self.w, today)
        body.update(
            {
                "reporter": self.w["stranger"].id,
                "reported_by": self.w["stranger"].id,
                "submitted_by": self.w["stranger"].id,
            }
        )
        res = self.client.post(self.url, body, format="json")
        assert res.status_code == 201, res.data
        assert res.data["reported_by"] == self.w["engineer"].id
        assert res.data["submitted_by"] == self.w["engineer"].id

    def test_w3_checklist_and_flagged_ride_along(self):
        """SUBMITTED incomplete → 400; medication lines → FLAGGED."""
        today = datetime.now(TEHRAN).date()
        bad = self._flutter_body(
            self.w, today.isoformat(), is_draft=False,
            checklist={"feed": True, "water": False, "device": True},
        )
        res = self.client.post(self.url, bad, format="json")
        assert res.status_code == 400, res.data
        assert "checklist incomplete" in res.data["error"]
        med = self._flutter_body(
            self.w, (today - timedelta(days=1)).isoformat(),
            medications=[{"medication_name": "oxytet", "dosage": "1ml", "frequency": "daily"}],
        )
        res = self.client.post(self.url, med, format="json")
        assert res.status_code == 201, res.data
        assert res.data["status"] == "FLAGGED"


class W4EditTests(AliasBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)

    def test_w4_alias_patch(self):
        today = datetime.now(TEHRAN).date().isoformat()
        created = self._canon_post(self.client, self.w, today)
        url = f"/api/compat/v1/reports/{created.data['id']}/"
        res = self.client.patch(
            url,
            {"casualties_today": 7, "ventilation_status": "پایدار",
             "checklist": {"feed": True, "water": True, "device": True}},
            format="json",
        )
        assert res.status_code == 200, res.data
        assert res.data["mortality_count"] == 7
        assert res.data["ventilation_status"] == "STABLE"

    def test_w4_checklist_incomplete_400(self):
        """In-play checklist scoping rides along: SUBMITTED target with an
        incomplete checklist → 400 through the compat PATCH too."""
        today = datetime.now(TEHRAN).date().isoformat()
        created = self._canon_post(
            self.client, self.w, today, status="DRAFT",
            checklist={"feed": True, "water": True, "device": False},
        )
        url = f"/api/compat/v1/reports/{created.data['id']}/"
        res = self.client.patch(url, {"status": "SUBMITTED"}, format="json")
        assert res.status_code == 400, res.data
        assert "checklist incomplete" in res.data["error"]

    def test_w4_unknown_report_404(self):
        res = self.client.patch("/api/compat/v1/reports/999999999/", {"notes": "x"}, format="json")
        assert res.status_code == 404, res.data
        assert "error" in res.data


class W5FarmQueueTests(AliasBaseTests):
    def setUp(self):
        self.client = APIClient()
        self.w = self._make_world(uuid.uuid4().hex[:8])
        _login(self.client, self.w["engineer"].username)

    def test_w5_spans_farm_saloons_excludes_other_farm(self):
        """W5 lists every saloon of the farm; farm2's reports never leak in;
        ?status=incomplete keeps DRAFT+SUBMITTED only."""
        w = self.w
        today = datetime.now(TEHRAN).date()
        full = {"feed": True, "water": True, "device": True}
        in_a = self._canon_post(self.client, w, today.isoformat(), status="DRAFT")
        canon_b_url = reverse("reports:saloon-reports", args=[w["saloon_b"].id])
        res_b = self.client.post(
            canon_b_url,
            {"flock_id": w["flock_b"].flock_id,
             "report_date": (today - timedelta(days=1)).isoformat(),
             "mortality_count": 1, "status": "SUBMITTED", "checklist": dict(full)},
            format="json",
        )
        assert res_b.status_code == 201, res_b.data
        # A real report on farm2's saloon (own flock) must never leak into
        # farm1's queue.
        flock_f2 = FlockCycle.objects.create(
            org=w["org"], saloon=w["saloon_f2"], breed_type="ross",
            start_date=datetime.now(TEHRAN) - timedelta(days=5), placement_count=700,
        )
        res_f2 = self.client.post(
            reverse("reports:saloon-reports", args=[w["saloon_f2"].id]),
            {"flock_id": flock_f2.flock_id,
             "report_date": today.isoformat(), "mortality_count": 9},
            format="json",
        )
        assert res_f2.status_code == 201, res_f2.data
        got = self.client.get(self._w5(w))
        assert got.status_code == 200, got.data
        assert isinstance(got.data, list)
        assert {r["id"] for r in got.data} == {in_a.data["id"], res_b.data["id"]}
        assert res_f2.data["id"] not in {r["id"] for r in got.data}
        inc = self.client.get(self._w5(w), {"status": "incomplete"})
        assert inc.status_code == 200, inc.data
        assert {r["id"] for r in inc.data} == {in_a.data["id"], res_b.data["id"]}

    def test_w5_foreign_slug_404_and_inverted_400(self):
        res = self.client.get(
            f"/api/compat/v1/farms/{self.w['slugs']['farm_foreign']}/reports/"
        )
        assert res.status_code == 404, res.data
        assert "error" in res.data
        today = datetime.now(TEHRAN).date()
        res = self.client.get(
            self._w5(self.w),
            {"from": today.isoformat(), "to": (today - timedelta(days=3)).isoformat()},
        )
        assert res.status_code == 400, res.data
        assert "invalid time window" in res.data["error"]
