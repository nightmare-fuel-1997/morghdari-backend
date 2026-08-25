"""M2-05 acceptance — TenantOrgMiddleware: order, verify-once, branches, GUC.

WHY: this middleware IS the tenancy mechanism for HTTP (Security §4.1 canon,
decisions D2/D3/D4). Proof layers:
- sqlite-safe: stack-order assertion, verify-once stash contract, TokenError
  logging (NR-4) with fail-closed pass-through.
- PostgreSQL-only (seeded_rls_db + transaction=True, mirroring
  test_tenant_context.py): each branch's REAL effect under FORCE RLS as
  morghdari_app — org-scoped reads, support-session precedence over the staff
  skip, platform-staff zero rows, org-less token fail-closed, C-12 residue.

فارسی: پذیرش M2-05 — ترتیب میان‌افزار، امضای یک‌باره، لاگِ توکن خراب و اثر واقعی
هر شاخه زیر RLS با نقش morghdari_app اثبات می‌شود.
"""

from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts.authentication import CachedJWTAuthentication
from apps.accounts.models import OrgMember
from apps.accounts.serializers import LoginSerializer
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.middleware import CLAIMS_ATTR, TOKEN_STASH_ATTR, TenantOrgMiddleware
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"


def _fetchone(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def _make_org_with_saloon(name: str):
    province = Province.objects.create(name=f"P-{name}")
    city = City.objects.create(province=province, name=f"C-{name}")
    org = Organization.objects.create(name=name)
    farm = Farm.objects.create(org=org, name=f"F-{name}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"S-{name}")
    return org, saloon


class _Probe:
    """Stand-in get_response recording what the request could actually see.

    read_guc=False for sqlite-safe layers (current_setting is Postgres-only);
    the real-GUC assertions belong to the postgres branch tests below.
    """

    def __init__(self, read_guc: bool = True):
        self.ran = False
        self.guc = None
        self.saloons_seen = None
        self.read_guc = read_guc

    def __call__(self, request):
        self.ran = True
        if self.read_guc:
            self.guc = _fetchone("SELECT current_setting('app.current_org_id', true)")
            self.saloons_seen = list(Saloon.objects.values_list("name", flat=True))
        return HttpResponse("ok")


def _middleware_call(probe, token: str | None):
    factory = RequestFactory()
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    request = factory.get("/api/v1/whatever/", **headers)
    response = TenantOrgMiddleware(probe)(request)
    return request, response


class StackOrderTests(TestCase):
    def test_tenant_middleware_runs_after_auth_middleware(self):
        # D3: the stash producer must run after Django's auth machinery;
        # misordering would silently break CachedJWTAuthentication reuse.
        auth_idx = settings.MIDDLEWARE.index(
            "django.contrib.auth.middleware.AuthenticationMiddleware"
        )
        tenant_idx = settings.MIDDLEWARE.index("apps.tenants.middleware.TenantOrgMiddleware")
        self.assertGreater(tenant_idx, auth_idx)


class VerifyOnceTests(TestCase):
    def _login_access_for(self, username: str) -> str:
        User.objects.create_user(username=username, password=PASSWORD)
        serializer = LoginSerializer(data={"username": username, "password": PASSWORD})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.validated_data["access"]

    def test_signature_verified_exactly_once_per_request(self):
        # Branch-② shape (platform-staff claim): exercises validate+stash+reuse
        # WITHOUT any DB-specific GUC call, so it runs on every laptop loop.
        # Branch ③'s verify-once is additionally spied in the postgres tests.
        user = User.objects.create_user(username="staffer", password=PASSWORD)
        user.is_platform_staff = True
        user.save(update_fields=["is_platform_staff"])
        token = AccessToken.for_user(user)
        token["is_platform_staff"] = True

        probe = _Probe(read_guc=False)
        with patch("apps.tenants.middleware.AccessToken") as spy:
            # Delegate to the REAL class (call it, not its bare __init__).
            spy.side_effect = lambda *a, **kw: AccessToken(*a, **kw)
            request, _response = _middleware_call(probe, str(token))
            # DRF layer reuses the stash: no second signature verification.
            auth_user, _validated = CachedJWTAuthentication().authenticate(request)
            self.assertEqual(spy.call_count, 1)

        self.assertTrue(probe.ran)
        self.assertEqual(auth_user.id, user.id)

    def test_garbage_token_is_logged_and_fails_closed(self):
        probe = _Probe(read_guc=False)
        with self.assertLogs("apps.tenants.middleware", level="WARNING") as logs:
            request, _response = _middleware_call(probe, "not-a-jwt")
        self.assertTrue(any("JWT rejected" in line for line in logs.output))  # NR-4
        self.assertTrue(probe.ran)
        self.assertFalse(hasattr(request, CLAIMS_ATTR))
        self.assertFalse(hasattr(request, TOKEN_STASH_ATTR))
        # Fail-closed at the DRF layer too: protected views get 401, not a leak.
        with self.assertRaises(AuthenticationFailed):
            CachedJWTAuthentication().authenticate(request)


# ---------------------------------------------------------------------------
# Real-RLS proofs — postgres only; sqlite laptop loops skip via conftest.
# ---------------------------------------------------------------------------

PG_MARKS = [pytest.mark.use_migrations, pytest.mark.django_db(transaction=True)]


def _as_app_role():
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")


def _reset_role():
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_pg_tenant_token_scopes_reads_and_guc_dies_at_commit(seeded_rls_db):
    org_a, _ = _make_org_with_saloon("A")
    _make_org_with_saloon("B")
    user = User.objects.create_user(username="worker-a", password=PASSWORD)
    OrgMember.objects.create(user=user, org=org_a, role="worker", is_active=True)
    serializer = LoginSerializer(data={"username": "worker-a", "password": PASSWORD})
    assert serializer.is_valid(), serializer.errors
    access = serializer.validated_data["access"]

    probe = _Probe()
    _as_app_role()
    try:
        with patch("apps.tenants.middleware.AccessToken") as spy:
            spy.side_effect = lambda *a, **kw: AccessToken(*a, **kw)
            request, _response = _middleware_call(probe, access)
            # Verify-once holds on the REAL tenant path too: DRF reuse adds no
            # second signature check.
            auth_user, _validated = CachedJWTAuthentication().authenticate(request)
            assert spy.call_count == 1
        assert probe.ran
        assert auth_user.username == "worker-a"
        assert probe.guc == str(org_a.org_id)  # branch ③ set the GUC from the claim
        assert probe.saloons_seen == ["S-A"]  # RLS scoped the read to org A

        # C-12 regression guard: COMMIT emptied the tx-local GUC on this pooled
        # connection; NULLIF-guarded policy ⇒ fail-closed zero rows again.
        assert _fetchone("SELECT current_setting('app.current_org_id', true)") == ""
        assert _fetchone("SELECT count(*) FROM saloons") == 0
    finally:
        _reset_role()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_pg_support_session_beats_platform_staff_skip(seeded_rls_db):
    _make_org_with_saloon("A")
    org_b, _ = _make_org_with_saloon("B")
    staff = User.objects.create_user(username="supporter", password=PASSWORD)
    staff.is_platform_staff = True
    staff.save(update_fields=["is_platform_staff"])
    token = AccessToken.for_user(staff)
    token["support_session"] = True
    token["incident_id"] = "INC-1"
    token["is_platform_staff"] = True
    token["org_id"] = str(org_b.org_id)

    probe = _Probe()
    _as_app_role()
    try:
        request, _response = _middleware_call(probe, str(token))
        assert probe.ran
        # Branch ① ran DESPITE is_platform_staff (§4.5 precedence): exactly B.
        assert request.jwt_claims["incident_id"] == "INC-1"
        assert probe.guc == str(org_b.org_id)
        assert probe.saloons_seen == ["S-B"]
    finally:
        _reset_role()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_pg_platform_staff_without_support_session_sees_zero_rows(seeded_rls_db):
    _make_org_with_saloon("A")
    staff = User.objects.create_user(username="adminish", password=PASSWORD)
    staff.is_platform_staff = True
    staff.save(update_fields=["is_platform_staff"])
    token = AccessToken.for_user(staff)
    token["is_platform_staff"] = True  # our serializer emits this claim when true

    probe = _Probe()
    _as_app_role()
    try:
        request, _response = _middleware_call(probe, str(token))
        assert probe.ran  # ② passed through — the view runs, it just sees nothing
        assert getattr(request, TOKEN_STASH_ATTR, None) is not None  # still verified once
        # NO GUC set by branch ②: NULL before any prior set on this pooled
        # conn, empty string after a prior tx-local commit (C-12) — either
        # way the policy matches zero rows.
        assert not probe.guc
        assert probe.saloons_seen == []  # FORCE RLS ⇒ zero rows, fail-closed
    finally:
        _reset_role()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_pg_valid_but_orgless_token_passes_through_fail_closed(seeded_rls_db):
    _make_org_with_saloon("A")
    user = User.objects.create_user(username="bare", password=PASSWORD)
    access = str(RefreshToken.for_user(user).access_token)  # no org claim by construction

    probe = _Probe()
    _as_app_role()
    try:
        request, _response = _middleware_call(probe, access)
        assert probe.ran  # ④ pass-through
        assert hasattr(request, CLAIMS_ATTR)
        assert "org_id" not in request.jwt_claims
        # ④ never touched the GUC: NULL or C-12 empty-string, but never a value.
        assert not probe.guc
        assert probe.saloons_seen == []  # authenticated-looking, sees nothing
    finally:
        _reset_role()
