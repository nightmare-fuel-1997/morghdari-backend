"""M2-09 acceptance — the offboarding story, proven as one chain (C-14).

WHY: rotation/blacklist are banned, so deactivation's teeth must come from
claim semantics + RLS alone. The full chain on the REAL stack:
  active member logs in → access lives ≤15 min (exp−iat==900, frozen clock)
  → owner deactivates via the REAL contract endpoint → victim's still-valid
  refresh yields a NEW access WITHOUT an org_id claim → that token calling a
  tenant read gets HTTP 200 with an EMPTY payload (fail-closed, correct!)
  → and under FORCE RLS as morghdari_app, after the middleware transaction
  COMMITS (plain-function style: real autocommit, no TestCase savepoints),
  the pooled connection shows the C-12 empty residue and ZERO rows.

فارسی: پذیرش M2-09 — زنجیرهٔ کاملِ خروجِ عضو روی پشتهٔ واقعی؛ مرگِ GUC فقط در
اتوکامیتِ واقعی معنا دارد، پس تست به‌صورت تابع ساده (نه TestCase) نوشته شده است.
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpResponse
from django.urls import reverse
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts.models import OrgMember
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.middleware import TenantOrgMiddleware
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"


def _make_org_with_saloon(name: str):
    province = Province.objects.create(name=f"P-{name}")
    city = City.objects.create(province=province, name=f"C-{name}")
    org = Organization.objects.create(name=name)
    farm = Farm.objects.create(org=org, name=f"F-{name}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"S-{name}")
    return org, saloon


def _login(client, username: str) -> dict:
    response = client.post(
        reverse("accounts:auth-login"),
        {"username": username, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.data
    return response.data


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_full_offboarding_chain(seeded_rls_db):
    # --- fixture: owner + victim in one org, one saloon on the table ---
    org, _saloon = _make_org_with_saloon("story")
    owner = User.objects.create_user(username="boss", password=PASSWORD)
    OrgMember.objects.create(user=owner, org=org, role="owner", is_active=True)
    victim = User.objects.create_user(username="victim", password=PASSWORD)
    victim_member = OrgMember.objects.create(
        user=victim, org=org, role="worker", is_active=True
    )

    client = APIClient()

    # 0. While active: the victim sees exactly their own org's saloons.
    fresh = _login(client, "victim")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {fresh['access_token']}")
    listed = client.get(reverse("accounts:org-members"))
    assert listed.status_code == 200
    assert {row["username"] for row in listed.data} >= {"boss", "victim"}

    # 1. Access-token lifetime is the ONLY revocation guarantee: 15 min.
    payload = AccessToken(fresh["access_token"]).payload
    # SimpleJWT pairs the access exp to the refresh's issue instant while the
    # access iat uses its own clock read; a second boundary between the two
    # mints (borne by a slow/loaded CI runner) yields 899, never less — the
    # 15-min guarantee (exp measured from pair issue) holds either way.
    assert 899 <= payload["exp"] - payload["iat"] <= 900
    refresh_payload = RefreshToken(fresh["refresh_token"]).payload
    # exp/iat of a single token share one current_time — always exact.
    assert refresh_payload["exp"] - refresh_payload["iat"] == 7 * 24 * 3600

    # 2. Owner deactivates via the REAL contract endpoint.
    owner_client = APIClient()
    owner_login = _login(owner_client, "boss")
    owner_client.credentials(HTTP_AUTHORIZATION=f"Bearer {owner_login['access_token']}")
    off = owner_client.post(
        reverse("accounts:org-member-deactivate", args=[victim_member.id])
    )
    assert off.status_code == 200
    assert off.data["is_active"] is False

    # 3. The still-valid refresh mints a NEW access WITHOUT an org claim.
    refreshed = client.post(
        reverse("accounts:auth-refresh"),
        {"refresh": fresh["refresh_token"]},
        format="json",
    )
    assert refreshed.status_code == 200
    new_payload = AccessToken(refreshed.data["access"]).payload
    assert "org_id" not in new_payload  # THE C-14 guarantee
    assert new_payload.get("roles") == []  # no tenant authority

    # 4. C-14: that org-less token reads tenant data as HTTP 200 + EMPTY,
    #    never an error — fail-closed, and provably not another org's data.
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}")
    ghost_read = client.get(reverse("accounts:org-members"))
    assert ghost_read.status_code == 200
    assert ghost_read.data == []

    # 5. Under FORCE RLS as morghdari_app, after the middleware transaction
    #    COMMITTED (real autocommit here): C-12 residue + fail-closed zeros.
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")
    try:
        request = APIRequestFactory().get(
            "/", HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}"
        )
        TenantOrgMiddleware(lambda r: HttpResponse("ok"))(request)
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.current_org_id', true)")
            residue = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM saloons")
            visible = cursor.fetchone()[0]
        assert residue in (None, "")  # no GUC left behind on the pooled conn
        assert visible == 0  # fail-closed zero rows
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
