"""M6-10 acceptance — ops wave (contract v1.7.0, O1–O12; docs/api-ops-v1.md).

Gates (spec §6, adapted to the REAL v1.5.0 schema where the spec's sketch
differed — see module docstrings in ops_views.py):
- O1–O12 answer with EXACT contractual keys; every list is {results, count}.
- Role matrix: worker 403 on O1/O3-write/O7; engineer 403 on O3-write; any
  member 200 on O5-read. Explicit role comparisons are the portable fence
  (RLS is the backstop — M6-06 lesson).
- Foreign-id 404s are identical to missing (never an existence oracle).
- O10: wrong current → 401; weak/new-equals-old → 400; 4th call in a minute
  → 429 (auth_strict). Refresh register: revoke ⇒ refresh line dies (jti).
- O9 store: diagnostics-only payloads (Firmware §3.8) survive the pipeline
  (previously silent death at Gate 5) and land in diag:last:{slot}; telemetry
  path untouched. Real Redis when available.

فارسی: ماتریسِ نقش‌ها، کلیدهای قراردادی، حفاظِ جعلِ وجود، و تله‌متریِ دست‌نخورده.
"""

import json

import pyotp
import pytest
import redis as redis_lib
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIRequestFactory, APIClient

try:  # real Redis for the diag register when available (PG + laptop loop)
    from tests.conftest import INTEGRATION_REDIS_URL
except Exception:  # pragma: no cover - conftest always present in practice
    INTEGRATION_REDIS_URL = None

from apps.accounts.models import OrgMember, UserSession
from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule
from apps.auditing.models import AccountActivityLog
from apps.devices.models import (
    GatewayHardwareRegistry,
    GatewaySaloonZone,
    GatewaySlot,
    SaloonPhoto,
)
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.monitoring import ops_services
from apps.telemetry.models import FlockCycle
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# World builder
# ---------------------------------------------------------------------------


def _make_world():
    province = Province.objects.create(name="OpsP")
    city = City.objects.create(province=province, name="OpsC")
    org = Organization.objects.create(name="ops-org")
    other = Organization.objects.create(name="ops-org-b")
    farm = Farm.objects.create(org=org, name="OpsF", province=province, city=city)
    farm_b = Farm.objects.create(org=org, name="OpsF-b", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="OpsS")
    saloon_b = Saloon.objects.create(org=org, farm=farm_b, name="OpsS-b")

    def member(username, role, org_row=org):
        user = User.objects.create_user(username=username, password=PASSWORD)
        return OrgMember.objects.create(user=user, org=org_row, role=role, is_active=True)

    users = {r: member(f"ops-{r}", r) for r in ("owner", "manager", "engineer", "worker")}
    foreign = member("ops-foreign-owner", "owner", org_row=other)

    slot = GatewaySlot.objects.create(slot_id="GW-OPS-1", org=org, farm=farm)
    slot_b = GatewaySlot.objects.create(slot_id="GW-OPS-2", org=org, farm=farm_b)
    slot_foreign = GatewaySlot.objects.create(slot_id="GW-FRN-1", org=other, farm=None)
    GatewaySaloonZone.objects.create(gateway_slot=slot, saloon=saloon, org=org)
    GatewayHardwareRegistry.objects.create(hardware_id="HW-OPS-A", slot=slot, org=org)

    # The world's OWN template (org-owned): O2 reads it plus globals; O3's
    # write scope (own-org templates ONLY) is exercisable against it.
    template = LifecycleTemplate.objects.create(name="OpsTpl", breed_type="ross", org=org)
    rule = LifecycleThresholdRule.objects.create(
        template=template, day_from=1, day_to=10, metric="temp",
        min_value=20, max_value=30,
    )
    LifecycleThresholdRule.objects.create(
        template=template, day_from=11, day_to=20, metric="nh3",
        min_value=0, max_value=25,
    )
    flock = FlockCycle.objects.create(
        org=org, saloon=saloon, breed_type="ross",
        start_date="2026-09-01T00:00:00Z", placement_count=100,
    )
    log = AccountActivityLog.objects.create(
        actor=users["owner"].user, org=org, farm=farm, saloon=saloon,
        action="DEMO_ACTION", details={"k": "v"},
    )

    return {
        "org": org, "other": other, "farm": farm, "farm_b": farm_b,
        "saloon": saloon, "saloon_b": saloon_b,
        "users": users, "foreign": foreign,
        "slot": slot, "slot_b": slot_b, "slot_foreign": slot_foreign,
        "template": template, "rule": rule, "flock": flock, "log": log,
    }


def _login(username, password=PASSWORD):
    client = APIClient()
    body = {"username": username, "password": password}
    row = User.objects.filter(username=username).first()
    # M7-07: MFA-gated staff logins must present a live TOTP code.
    if row is not None and getattr(row, "platform_mfa", None) is not None:
        body["mfa_code"] = pyotp.TOTP(row.platform_mfa.secret).now()
    res = client.post(
        reverse("accounts:auth-login"),
        body,
        format="json",
    )
    assert res.status_code == 200, res.content
    return res.data["access_token"]


def _client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


# ---------------------------------------------------------------------------
# O1 — audit-logs
# ---------------------------------------------------------------------------


class TestO1AuditLogs:
    def test_exact_keys_and_envelope(self):
        _make_world()
        res = _client(_login("ops-owner")).get(reverse("ops:audit-logs"))
        assert res.status_code == 200
        assert set(res.data.keys()) == {"results", "count"}
        assert res.data["count"] == 1
        row = res.data["results"][0]
        assert set(row.keys()) == {"id", "actor_id", "username", "action", "details", "created_at"}
        assert row["action"] == "DEMO_ACTION"
        assert row["username"] == "ops-owner"

    def test_filters_and_pagination(self):
        w = _make_world()
        AccountActivityLog.objects.create(
            actor=w["users"]["owner"].user, org=w["org"], action="OTHER_ACTION"
        )
        client = _client(_login("ops-owner"))

        res = client.get(reverse("ops:audit-logs"), {"action": "OTHER_ACTION"})
        assert res.data["count"] == 1 and res.data["results"][0]["action"] == "OTHER_ACTION"

        res = client.get(reverse("ops:audit-logs"), {"limit": 1, "offset": 0})
        assert len(res.data["results"]) == 1 and res.data["count"] == 2

        res = client.get(reverse("ops:audit-logs"), {"limit": -1})
        assert res.status_code == 400 and res.data == {"error": "invalid pagination"}
        res = client.get(reverse("ops:audit-logs"), {"limit": "abc"})
        assert res.status_code == 400

    def test_role_matrix_worker_forbidden(self):
        _make_world()
        res = _client(_login("ops-worker")).get(reverse("ops:audit-logs"))
        assert res.status_code == 403 and res.data == {"error": "forbidden"}

    def test_foreign_log_invisible(self):
        w = _make_world()
        AccountActivityLog.objects.create(
            actor=w["foreign"].user, org=w["other"], action="FOREIGN_ACTION"
        )
        res = _client(_login("ops-owner")).get(reverse("ops:audit-logs"))
        assert res.data["count"] == 1  # only own-org row

    def test_bad_from_date_is_400(self):
        _make_world()
        res = _client(_login("ops-owner")).get(
            reverse("ops:audit-logs"), {"from": "not-a-date"}
        )
        assert res.status_code == 400


# ---------------------------------------------------------------------------
# O2/O3 — thresholds
# ---------------------------------------------------------------------------


class TestO2O3Thresholds:
    def test_list_exact_keys(self):
        _make_world()
        res = _client(_login("ops-engineer")).get(reverse("ops:thresholds"))
        assert res.status_code == 200
        assert set(res.data.keys()) == {"results", "count"}
        assert res.data["count"] == 2
        row = res.data["results"][0]
        # Real-schema row: the draft's `enabled`/`updated_at` have no columns
        # in the frozen table (ratifying-yaml erratum; see threshold_item).
        assert set(row.keys()) == {
            "id", "metric_key", "template", "day_from", "day_to", "min", "max",
        }

    def test_saloon_param_filters_by_breed(self):
        w = _make_world()
        res = _client(_login("ops-engineer")).get(
            reverse("ops:thresholds"), {"saloon_id": w["saloon"].pk}
        )
        assert res.data["count"] == 2  # both rules belong to the ross template
        res = _client(_login("ops-engineer")).get(
            reverse("ops:thresholds"), {"saloon_id": w["saloon_b"].pk}
        )
        assert res.data == {"results": [], "count": 0}  # flock-less saloon

    def test_worker_forbidden(self):
        _make_world()
        res = _client(_login("ops-worker")).get(reverse("ops:thresholds"))
        assert res.status_code == 403

    def test_update_manager_ok_engineer_forbidden(self):
        w = _make_world()
        url = reverse("ops:threshold-update", args=[w["rule"].id])
        res = _client(_login("ops-manager")).put(url, {"max": 32}, format="json")
        assert res.status_code == 200
        assert res.data["max"] == 32.0
        w["rule"].refresh_from_db()
        assert str(w["rule"].max_value) == "32.00"
        assert AccountActivityLog.objects.filter(action="THRESHOLD_UPDATED", org=w["org"]).exists()
        res = _client(_login("ops-engineer")).put(url, {"max_value": 33}, format="json")
        assert res.status_code == 403  # engineer+ read, manager+ write

    def test_update_out_of_range_400(self):
        w = _make_world()
        url = reverse("ops:threshold-update", args=[w["rule"].id])
        client = _client(_login("ops-manager"))
        res = client.put(url, {"max_value": 999}, format="json")  # > temp bound 80
        assert res.status_code == 400 and res.data == {"error": "threshold out of range"}
        res = client.put(url, {"min_value": 50, "max_value": 10}, format="json")
        assert res.status_code == 400  # min >= max
        res = client.put(url, {"min_value": "not-a-number"}, format="json")
        assert res.status_code == 400

    def test_update_foreign_template_rule_404(self):
        """A rule of a FOREIGN org's template — and every global/orphan rule —
        is OUTSIDE the write scope → 404 (never an oracle: same shape as
        unknown). Global catalog rows are readable via O2, writable by NO ONE."""
        w = _make_world()
        foreign_template = LifecycleTemplate.objects.create(
            name="ForeignTpl", breed_type="ross", org=w["other"]
        )
        foreign_rule = LifecycleThresholdRule.objects.create(
            template=foreign_template, day_from=1, day_to=2, metric="temp",
            min_value=1, max_value=2,
        )
        orphan_rule = LifecycleThresholdRule.objects.create(
            template=None, day_from=1, day_to=2, metric="temp", min_value=1, max_value=2
        )
        client = _client(_login("ops-owner"))
        for target in (foreign_rule, orphan_rule):
            res = client.put(
                reverse("ops:threshold-update", args=[target.id]), {"max_value": 99}, format="json"
            )
            assert res.status_code == 404

    def test_update_unknown_rule_404(self):
        _make_world()
        res = _client(_login("ops-manager")).put(
            reverse("ops:threshold-update", args=[999999]), {"max_value": 1}, format="json"
        )
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# O4 — threshold templates
# ---------------------------------------------------------------------------


class TestO4Templates:
    def test_list_and_breed_filter(self):
        w = _make_world()
        LifecycleTemplate.objects.create(name="T2", breed_type="legacy")
        res = _client(_login("ops-engineer")).get(reverse("ops:threshold-templates"))
        assert res.status_code == 200 and res.data["count"] == 2
        row = res.data["results"][0]
        assert set(row.keys()) == {"template_id", "org_id", "breed_type", "name", "is_default"}
        res = _client(_login("ops-engineer")).get(
            reverse("ops:threshold-templates"), {"breed_type": "legacy"}
        )
        assert res.data["count"] == 1 and res.data["results"][0]["breed_type"] == "legacy"

    def test_worker_forbidden(self):
        _make_world()
        res = _client(_login("ops-worker")).get(reverse("ops:threshold-templates"))
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# O5/O6 — photos
# ---------------------------------------------------------------------------


class TestO5O6Photos:
    def test_list_envelope(self):
        w = _make_world()
        SaloonPhoto.objects.create(
            saloon=w["saloon"], org=w["org"], uploaded_by=w["users"]["worker"].user,
            file_url="media/x.jpg", description="cap",
        )
        # Any member of the owning org may read (spec: O5 role = any member).
        for username in ("ops-worker", "ops-owner"):
            res = _client(_login(username)).get(reverse("ops:saloon-photos", args=[w["saloon"].pk]))
            assert res.status_code == 200
            assert set(res.data.keys()) == {"results", "count"}
            row = res.data["results"][0]
            assert set(row.keys()) == {"id", "url", "caption", "taken_at", "uploaded_by"}
            assert row["url"] == "media/x.jpg"
            assert row["caption"] == "cap"

    def test_foreign_saloon_is_404(self):
        w = _make_world()
        saloon_other = Saloon.objects.create(org=w["other"], farm=None, name="S-other")
        res = _client(_login("ops-worker")).get(reverse("ops:saloon-photos", args=[saloon_other.pk]))
        assert res.status_code == 404

    def test_upload_engineer_ok_worker_403_bad_type_400(self):
        w = _make_world()
        url = reverse("ops:saloon-photos", args=[w["saloon"].pk])
        good = SimpleUploadedFile("a.jpg", b"\xff\xd8\xff\xe0fake", content_type="image/jpeg")
        res = _client(_login("ops-engineer")).post(url, {"file": good}, format="multipart")
        assert res.status_code == 201, res.content
        assert set(res.data.keys()) == {"id", "url", "caption", "taken_at", "uploaded_by"}

        good2 = SimpleUploadedFile("b.jpg", b"\xff\xd8\xff\xe0fake", content_type="image/jpeg")
        res = _client(_login("ops-worker")).post(url, {"file": good2}, format="multipart")
        assert res.status_code == 403

        bad = SimpleUploadedFile("c.txt", b"hello", content_type="text/plain")
        res = _client(_login("ops-engineer")).post(url, {"file": bad}, format="multipart")
        assert res.status_code == 400

        missing = _client(_login("ops-engineer")).post(url, {}, format="multipart")
        assert missing.status_code == 400


# ---------------------------------------------------------------------------
# O7/O8 — gateways
# ---------------------------------------------------------------------------


class TestO7O8Gateways:
    def test_list_keys_filters_pagination(self):
        w = _make_world()
        client = _client(_login("ops-manager"))
        res = client.get(reverse("ops:gateways"))
        assert res.status_code == 200
        assert set(res.data.keys()) == {"results", "count"}
        assert res.data["count"] == 2
        row = res.data["results"][0]
        assert set(row.keys()) == {
            "slot_id", "farm_id", "provisioning_status", "last_seen_at", "saloon_count",
        }
        res = client.get(reverse("ops:gateways"), {"farm_id": str(w["farm"].pk)})
        assert res.data["count"] == 1 and res.data["results"][0]["slot_id"] == "GW-OPS-1"
        res = client.get(reverse("ops:gateways"), {"status": "PROVISIONED"})
        assert res.data["count"] == 0

    def test_worker_forbidden(self):
        _make_world()
        res = _client(_login("ops-worker")).get(reverse("ops:gateways"))
        assert res.status_code == 403

    def test_detail_zones_and_active_hardware(self):
        w = _make_world()
        res = _client(_login("ops-manager")).get(reverse("ops:gateway-detail", args=["GW-OPS-1"]))
        assert res.status_code == 200
        body = res.data
        assert set(body.keys()) == {
            "slot_id", "farm_id", "provisioning_status", "last_seen_at",
            "saloon_count", "saloon_zones", "active_hardware_id",
        }
        assert body["saloon_count"] == 1
        assert body["saloon_zones"][0]["saloon_id"] == w["saloon"].pk
        assert body["active_hardware_id"] == "HW-OPS-A"

    def test_unknown_and_foreign_identical_404(self):
        _make_world()
        client = _client(_login("ops-manager"))
        for slot_id in ("GW-NOPE", "GW-FRN-1"):
            res = client.get(reverse("ops:gateway-detail", args=[slot_id]))
            assert res.status_code == 404 and res.data == {"error": "gateway not found"}


# ---------------------------------------------------------------------------
# O9 — diagnostics (diag:last:{slot})
# ---------------------------------------------------------------------------


class TestO9Diagnostics:
    def test_capture_via_real_pipeline_then_serve(self, monkeypatch):
        w = _make_world()
        if INTEGRATION_REDIS_URL is None:
            pytest.skip("integration Redis unavailable")
        client = redis_lib.Redis.from_url(INTEGRATION_REDIS_URL, decode_responses=True)
        client.delete("diag:last:GW-OPS-1")
        # The view must read the SAME Redis the test wrote to (house pattern:
        # monkeypatch the module-level client factory, m6-09 style).
        monkeypatch.setattr("apps.monitoring.ops_services._redis_client", lambda: client)

        from morghdari.subscriber.pipeline import handle_message

        verdict = handle_message(
            client,
            f"{w['org'].org_id}/farms/{w['farm'].pk}/gateways/GW-OPS-1/telemetry",
            json.dumps({
                "schema_version": "2.0",
                "gateway_slot_id": "GW-OPS-1",
                "device_ts": "2026-09-06T10:00:00Z",
                "diagnostics": {
                    "battery_v": 12.4, "rssi": -67, "free_heap": 120000,
                    "sd_ok": "ok", "errors": ["sensor_3_timeout"],
                },
            }).encode(),
        )
        assert verdict is None  # SUCCESS — no longer a silent Gate-5 death

        res = _client(_login("ops-engineer")).get(
            reverse("ops:gateway-diagnostics", args=["GW-OPS-1"])
        )
        assert res.status_code == 200
        assert set(res.data.keys()) == {
            "slot_id", "reported_at", "battery_v", "rssi", "free_heap", "sd_ok", "errors",
        }
        assert res.data["battery_v"] == 12.4
        assert res.data["sd_ok"] is True
        assert res.data["errors"] == ["sensor_3_timeout"]
        assert res.data["reported_at"] == "2026-09-06T10:00:00Z"

    def test_worker_forbidden_and_never_reported_404(self):
        _make_world()
        res = _client(_login("ops-worker")).get(
            reverse("ops:gateway-diagnostics", args=["GW-OPS-1"])
        )
        assert res.status_code == 403
        # Gateway that never reported (or Redis down — same honest answer).
        res = _client(_login("ops-engineer")).get(
            reverse("ops:gateway-diagnostics", args=["GW-OPS-1"])
        )
        assert res.status_code == 404 and res.data == {"error": "no diagnostics yet"}

    def test_unknown_slot_404(self):
        _make_world()
        res = _client(_login("ops-engineer")).get(
            reverse("ops:gateway-diagnostics", args=["GW-NOPE"])
        )
        assert res.status_code == 404 and res.data == {"error": "gateway not found"}


# ---------------------------------------------------------------------------
# O10 — password change
# ---------------------------------------------------------------------------


class TestO10PasswordChange:
    @pytest.fixture(autouse=True)
    def _clean_throttle_cache(self):
        """auth_strict history lives in the SHARED Redis cache (db1) — a
        back-to-back pytest run within 60 s inherits the previous run's
        counters and 429s spuriously. Wipe it before every O10 test."""
        from django.core.cache import cache

        cache.clear()
        yield

    def test_happy_path(self):
        w = _make_world()
        res = _client(_login("ops-owner")).post(
            reverse("ops:password-change"),
            {"current_password": PASSWORD, "new_password": "brand-new-pass-9"},
            format="json",
        )
        assert res.status_code == 200
        w["users"]["owner"].user.refresh_from_db()
        assert w["users"]["owner"].user.check_password("brand-new-pass-9")

    def test_wrong_current_401(self):
        _make_world()
        res = _client(_login("ops-owner")).post(
            reverse("ops:password-change"),
            {"current_password": "nope", "new_password": "brand-new-pass-9"}, format="json",
        )
        assert res.status_code == 401

    def test_weak_and_same_400(self):
        _make_world()
        client = _client(_login("ops-manager"))
        res = client.post(
            reverse("ops:password-change"),
            {"current_password": PASSWORD, "new_password": PASSWORD}, format="json",
        )
        assert res.status_code == 400
        res = client.post(
            reverse("ops:password-change"),
            {"current_password": PASSWORD, "new_password": "short"}, format="json",
        )
        assert res.status_code == 400
        res = client.post(
            reverse("ops:password-change"),
            {"current_password": PASSWORD, "new_password": "password123"}, format="json",
        )
        assert res.status_code == 400

    def test_throttle_429_fourth_call(self):
        _make_world()
        client = _client(_login("ops-engineer"))
        for _ in range(3):
            res = client.post(
                reverse("ops:password-change"),
                {"current_password": "nope-wrong", "new_password": "whatever-pass-1"}, format="json",
            )
            assert res.status_code == 401
        res = client.post(
            reverse("ops:password-change"),
            {"current_password": "nope-wrong", "new_password": "whatever-pass-1"}, format="json",
        )
        assert res.status_code == 429


# ---------------------------------------------------------------------------
# O11/O12 — sessions
# ---------------------------------------------------------------------------


class TestO11O12Sessions:
    def test_login_creates_register_row(self):
        _make_world()  # creates ops-worker before the login below
        before = UserSession.objects.count()
        _login("ops-worker")
        assert UserSession.objects.count() == before + 1

    def test_register_prunes_dead_rows(self):
        """Retention bound (DB quality gate): the register stays a SLIDING
        token-lifetime window — register_session deletes the caller's rows
        whose token expired (created_at older than REFRESH_TOKEN_LIFETIME).
        SECURITY: a fresh REVOKED row must SURVIVE — revoked rows are the
        revocation register (refresh_is_revoked reads them); pruning them
        would un-revoke a still-circulating token. Other users' rows are
        untouched; live rows survive."""
        from datetime import timedelta

        from django.utils import timezone
        from rest_framework_simplejwt.settings import api_settings as jwt_api_settings

        w = _make_world()
        user = w["users"]["owner"].user
        expired = UserSession.objects.create(
            user=user,
            refresh_jti="expired-jti",
            created_at=timezone.now() - jwt_api_settings.REFRESH_TOKEN_LIFETIME - timedelta(minutes=1),
        )
        revoked = UserSession.objects.create(
            user=user, refresh_jti="revoked-jti", revoked_at=timezone.now()
        )
        others_live = UserSession.objects.create(
            user=w["users"]["worker"].user, refresh_jti="other-live-jti"
        )
        _login("ops-owner")  # register_session → prune_expired_sessions(owner)
        assert not UserSession.objects.filter(pk=expired.pk).exists()
        assert UserSession.objects.filter(pk=revoked.pk).exists()  # register
        assert UserSession.objects.filter(pk=others_live.pk).exists()

    def test_list_keys_and_current_flag(self):
        w = _make_world()
        token = _login("ops-owner")
        res = _client(token).get(reverse("ops:sessions"))
        assert res.status_code == 200
        assert len(res.data) == 1
        row = res.data[0]
        assert set(row.keys()) == {
            "id", "created_at", "last_seen_at", "ip", "user_agent", "current",
        }
        assert row["current"] is True  # access carries sid == the row's jti

    def test_refresh_touches_last_seen(self):
        _make_world()
        client = APIClient()
        res = client.post(
            reverse("accounts:auth-login"),
            {"username": "ops-owner", "password": PASSWORD}, format="json",
        )
        access, refresh = res.data["access_token"], res.data["refresh_token"]
        res = _client(access).post(reverse("accounts:auth-refresh"), {"refresh": refresh}, format="json")
        assert res.status_code == 200
        from rest_framework_simplejwt.tokens import RefreshToken

        jti = RefreshToken(refresh).payload["jti"]
        row = UserSession.objects.get(refresh_jti=jti)
        assert row.last_seen_at is not None

    def test_revoke_then_refresh_dies(self):
        _make_world()
        client = APIClient()
        res = client.post(
            reverse("accounts:auth-login"),
            {"username": "ops-owner", "password": PASSWORD}, format="json",
        )
        access, refresh = res.data["access_token"], res.data["refresh_token"]
        from rest_framework_simplejwt.tokens import RefreshToken

        session = UserSession.objects.get(refresh_jti=RefreshToken(refresh).payload["jti"])
        res = _client(access).delete(reverse("ops:session-revoke", args=[session.id]))
        assert res.status_code == 200
        res = _client(access).post(reverse("accounts:auth-refresh"), {"refresh": refresh}, format="json")
        assert res.status_code == 401  # the refresh line died

    def test_foreign_session_identical_404(self):
        w = _make_world()
        token = _login("ops-owner")
        stranger = User.objects.create_user(username="ops-stranger", password=PASSWORD)
        their_session = UserSession.objects.create(user=stranger, refresh_jti="foreign-jti-1")
        client = _client(token)
        res = client.delete(reverse("ops:session-revoke", args=[their_session.id]))
        assert res.status_code == 404
        res = client.delete(reverse("ops:session-revoke", args=[999999]))
        assert res.status_code == 404
        their_session.refresh_from_db()
        assert their_session.revoked_at is None  # untouched — no oracle

    def test_staff_orgless_can_use_sessions(self):
        _make_world()
        staff = User.objects.create_user(username="ops-staffer", password=PASSWORD)
        staff.is_platform_staff = True
        staff.save(update_fields=["is_platform_staff"])
        # M7-07: staff logins are MFA-gated — enroll so this suite tests
        # session listing, not the enrollment dance.
        from apps.accounts import mfa as mfa_mod

        mfa_row = mfa_mod.provision(staff)
        assert mfa_mod.confirm(staff, pyotp.TOTP(mfa_row.secret).now())
        res = _client(_login("ops-staffer")).get(reverse("ops:sessions"))
        assert res.status_code == 200


class TestOpsServicesUnits:
    """Pure-function edges of ops_services that the API tests above reach
    only partially (django-tdd audit: services target ≥90%): pagination
    caps, bad-scalar 400s, guard branches, D22 diag degradation/coercion."""

    @staticmethod
    def _rf(params):
        # Ops views run on DRF Request (query_params); wrap the WSGI request
        # exactly as @api_view does in production.
        from rest_framework.request import Request

        return Request(APIRequestFactory().get("/probe", params))

    def test_pagination_edges_and_cap(self):
        for params in ({"limit": "-1"}, {"offset": "-3"}, {"limit": "abc"}):
            with pytest.raises(ops_services.InvalidPagination):
                ops_services.parse_pagination(self._rf(params))
        assert ops_services.parse_pagination(self._rf({"limit": "9999", "offset": "5"})) == (500, 5)

    def test_page_and_paginated_envelope(self):
        items = list(range(10))
        assert ops_services.page(self._rf({"limit": "3", "offset": "2"}), items) == [2, 3, 4]
        env = ops_services.paginated(self._rf({"limit": "2", "offset": "8"}), items)
        assert env == {"results": [8, 9], "count": 10}

    def test_audit_filter_bad_scalars_are_400_never_500(self):
        for params in (
            {"actor_id": "x"}, {"farm_id": "nope"}, {"saloon_id": "zz"},
            {"from": "garbage"}, {"to": "2026-13-99"},
        ):
            with pytest.raises(ops_services.InvalidPagination):
                ops_services.build_audit_filters(Saloon.objects.none(), self._rf(params), None)

    def test_threshold_guard_branches(self):
        bad = ops_services.threshold_range_error
        assert bad("temp", {"min_value": "abc"}) is not None  # non-numeric
        assert bad("temp", {"min_value": 50, "max_value": 10}) is not None  # min >= max
        assert bad("temp", {"enabled": "yes"}) is not None  # non-bool enabled
        assert bad("temp", {"min_value": -999}) is not None  # below metric bound
        assert bad("temp", {"min_value": 20, "max_value": 30}) is None

    def test_photo_guard_branches(self):
        from types import SimpleNamespace

        assert ops_services.photo_error(None) == "file required"
        big = SimpleNamespace(size=11 * 1024 * 1024, content_type="image/jpeg")
        assert ops_services.photo_error(big) == "file too large (max 10 MB)"
        gif = SimpleNamespace(size=10, content_type="image/gif")
        assert ops_services.photo_error(gif) == "unsupported file type (jpg/png only)"
        png = SimpleNamespace(size=10, content_type="image/png")
        assert ops_services.photo_error(png) is None

    def test_diag_reader_degrades_to_none_and_coerces(self, monkeypatch):
        def boom():
            raise ConnectionError("redis down")

        monkeypatch.setattr(ops_services, "_redis_client", boom)
        assert ops_services.read_last_diagnostics("gw-x") is None  # D22: honest None

        class FakeClient:
            def __init__(self, mapping):
                self.mapping = mapping

            def hgetall(self, _key):
                return self.mapping

        monkeypatch.setattr(
            ops_services, "_redis_client",
            lambda: FakeClient({
                "reported_at": "2026-09-06T10:00:00Z", "battery_v": "12.4",
                "rssi": "", "free_heap": "oops", "sd_ok": "1", "errors": "[1, 2]",
            }),
        )
        assert ops_services.read_last_diagnostics("gw-y") == {
            "slot_id": "gw-y", "reported_at": "2026-09-06T10:00:00Z",
            "battery_v": 12.4, "rssi": None, "free_heap": None,
            "sd_ok": True, "errors": [1, 2],
        }

        monkeypatch.setattr(
            ops_services, "_redis_client",
            lambda: FakeClient({"sd_ok": "0", "errors": "{broken"}),
        )
        out = ops_services.read_last_diagnostics("gw-z")
        assert out["sd_ok"] is False
        assert out["errors"] == []

        monkeypatch.setattr(ops_services, "_redis_client", lambda: FakeClient({}))
        assert ops_services.read_last_diagnostics("gw-0") is None  # never reported


class TestSessionRegisterEdges:
    """The register is best-effort BY CONTRACT: a broken register/prune/touch
    must never break login or refresh, and claim-less tokens sit outside the
    register (django-tdd audit: these were the uncovered half of
    accounts/services.py)."""

    @staticmethod
    def _token(payload):
        from types import SimpleNamespace

        return SimpleNamespace(payload=payload)

    @staticmethod
    def _req(**meta):
        from types import SimpleNamespace

        return SimpleNamespace(META=dict(meta))

    def _user(self, username):
        return User.objects.create_user(username=username, password=PASSWORD)

    def test_register_without_jti_is_a_noop(self):
        from apps.accounts.services import register_session

        register_session(self._user("ops-edge-1"), self._token({}), self._req())
        assert UserSession.objects.count() == 0

    def test_register_stores_provenance_but_never_token_material(self):
        from apps.accounts.services import register_session

        user = self._user("ops-edge-2")
        register_session(
            user, self._token({"jti": "j-edge-2"}),
            self._req(REMOTE_ADDR="10.1.2.3", HTTP_USER_AGENT="flutter/1.0"),
        )
        row = UserSession.objects.get(refresh_jti="j-edge-2")
        assert row.user == user
        assert row.ip == "10.1.2.3"
        assert row.user_agent == "flutter/1.0"

    def test_register_failure_never_breaks_login(self, monkeypatch, caplog):
        import logging

        from apps.accounts.services import register_session

        def db_gone(*args, **kwargs):
            raise RuntimeError("db gone")

        monkeypatch.setattr(UserSession.objects, "update_or_create", db_gone)
        with caplog.at_level(logging.WARNING, logger="apps.accounts.services"):
            register_session(
                self._user("ops-edge-3"), self._token({"jti": "j-edge-3"}), self._req(),
            )
        assert "user_sessions register failed" in caplog.text

    def test_prune_failure_never_breaks_login(self, monkeypatch, caplog):
        import logging

        from apps.accounts.services import prune_expired_sessions
        from rest_framework_simplejwt.settings import api_settings as jwt_settings

        monkeypatch.setattr(jwt_settings, "REFRESH_TOKEN_LIFETIME", None)
        with caplog.at_level(logging.WARNING, logger="apps.accounts.services"):
            prune_expired_sessions(self._user("ops-edge-4"))
        assert "user_sessions prune failed" in caplog.text

    def test_touch_without_jti_or_unknown_jti_is_silent(self):
        from apps.accounts.services import touch_session

        touch_session(self._token({}))
        touch_session(self._token({"jti": "no-such-jti"}))
        assert UserSession.objects.count() == 0

    def test_touch_failure_never_breaks_refresh(self, monkeypatch, caplog):
        import logging

        from apps.accounts.services import touch_session

        def db_gone(*args, **kwargs):
            raise RuntimeError("db gone")

        monkeypatch.setattr(UserSession.objects, "filter", db_gone)
        with caplog.at_level(logging.WARNING, logger="apps.accounts.services"):
            touch_session(self._token({"jti": "j-edge-5"}))
        assert "user_sessions touch failed" in caplog.text

    def test_refresh_is_revoked_without_claims_is_false(self):
        from apps.accounts.services import refresh_is_revoked

        assert refresh_is_revoked(self._token({"foo": "bar"})) is False
