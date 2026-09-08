"""M2-10 acceptance — the v0.2 RBAC matrix in one parametrized sweep.

WHY: every (role, endpoint) verdict on the REAL middleware path, so no
permission decision lives only in an individual view test. Scope is strictly
v0.2-real endpoints: auth login/refresh, org-members list/invite/deactivate,
support-session. The TUTORIAL §3.6 /farms overview example is a FUTURE-version
URL and is deliberately absent.

Verdicts encoded (contract v1.4.4 + Security §4.5):
    read members      : any authenticated member 200 · staff org-less 200 [] · anon 401
    write members     : OWNER only (manager/worker/staff 403) · anon 401
    support session   : platform staff 200 · tenant roles + superuser 403 · anon 401
    login bad creds   : 401 regardless of identity

فارسی: کل ماتریس نقش×نقطه‌انتها روی مسیر واقعی میان‌افزار؛ کارگر جلوی درِ مدیر
۴۰۳، نوشتنِ اعضا فقط مالک، نشست پشتیبانی فقط کارکنان پلتفرم.
"""

import pyotp
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import OrgMember
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"


def _make_world():
    # get_or_create: this module runs under --reuse-db locally (CI's fresh DB
    # never collides) and re-runs _make_world per parametrized case in one
    # session — either way a second bare create() trips provinces.name UNIQUE.
    province, _ = Province.objects.get_or_create(name="MP")
    city, _ = City.objects.get_or_create(province=province, name="MC")
    org = Organization.objects.create(name="matrix-org")
    farm = Farm.objects.create(org=org, name="MF", province=province, city=city)
    Saloon.objects.create(org=org, farm=farm, name="MS")

    def member(username, role):
        user = User.objects.create_user(username=username, password=PASSWORD)
        return OrgMember.objects.create(user=user, org=org, role=role, is_active=True)

    owner_member = member("mx-owner", "owner")
    member("mx-manager", "manager")
    worker_member = member("mx-worker", "worker")

    staff = User.objects.create_user(username="mx-staffer", password=PASSWORD)
    staff.is_platform_staff = True
    staff.save(update_fields=["is_platform_staff"])

    superstaff = User.objects.create_user(
        username="mx-super", password=PASSWORD, is_superuser=True
    )
    superstaff.is_platform_staff = True
    superstaff.save(update_fields=["is_superuser", "is_platform_staff"])

    # M7-07: staff logins are MFA-gated — pre-enroll the staff actor so the
    # matrix (which predates MFA) tests RBAC, not the enrollment dance.
    from apps.accounts import mfa as mfa_mod

    mfa_row = mfa_mod.provision(staff)
    assert mfa_mod.confirm(staff, pyotp.TOTP(mfa_row.secret).now())

    return {
        "org": org,
        "owner_member": owner_member,
        "worker_member": worker_member,
        "tokens": {
            key: _login(key)
            for key in ("mx-owner", "mx-manager", "mx-worker", "mx-staffer", "mx-super")
        },
    }


def _login(username: str) -> str:
    client = APIClient()
    body = {"username": username, "password": PASSWORD}
    if username == "mx-staffer":  # the only MFA-enrolled actor in the matrix
        row = User.objects.get(username=username).platform_mfa
        body["mfa_code"] = pyotp.TOTP(row.secret).now()
    response = client.post(
        reverse("accounts:auth-login"),
        body,
        format="json",
    )
    assert response.status_code == 200, response.data
    return response.data["access_token"]


# (label, actor, url, method, payload-builder(world)->dict|None, expected)
MATRIX = [
    ("anon lists members", None, "members", "GET", lambda w: None, 401),
    ("anon invites", None, "invite", "POST", lambda w: None, 401),
    ("anon opens support session", None, "support", "POST", lambda w: None, 401),
    ("bad credentials login", "badpw", "login-bad", "POST", lambda w: None, 401),
    ("owner lists members", "mx-owner", "members", "GET", lambda w: None, 200),
    ("manager lists members", "mx-manager", "members", "GET", lambda w: None, 200),
    ("worker lists members", "mx-worker", "members", "GET", lambda w: None, 200),
    ("staff lists members (org-less empty)", "mx-staffer", "members", "GET", lambda w: None, 200),
    ("owner invites", "mx-owner", "invite", "POST", lambda w: {"username": "mx-new-1", "role": "worker"}, 201),
    ("manager invites", "mx-manager", "invite", "POST", lambda w: {"username": "mx-nope-2", "role": "worker"}, 403),
    ("worker invites", "mx-worker", "invite", "POST", lambda w: {"username": "mx-nope-3", "role": "worker"}, 403),
    ("staff invites", "mx-staffer", "invite", "POST", lambda w: {"username": "mx-nope-4", "role": "worker"}, 403),
    ("owner deactivates worker", "mx-owner", "deactivate-worker", "POST", lambda w: None, 200),
    ("manager deactivates worker", "mx-manager", "deactivate-worker", "POST", lambda w: None, 403),
    ("staff deactivates worker", "mx-staffer", "deactivate-worker", "POST", lambda w: None, 403),
    ("staff opens support session", "mx-staffer", "support", "POST",
     lambda w: {"org_id": str(w["org"].org_id), "incident_id": "MX-1"}, 200),
    ("superuser opens support session", "mx-super", "support", "POST",
     lambda w: {"org_id": str(w["org"].org_id), "incident_id": "MX-2"}, 403),
    ("owner opens support session", "mx-owner", "support", "POST",
     lambda w: {"org_id": str(w["org"].org_id), "incident_id": "MX-3"}, 403),
]

URL_NAMES = {
    "members": "accounts:org-members",
    "invite": "accounts:org-members",
    "deactivate-worker": "accounts:org-member-deactivate",
    "support": "accounts:support-session",
    "login-bad": "accounts:auth-login",
}


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("label,actor,url_key,method,payload,expected", MATRIX, ids=[r[0] for r in MATRIX])
def test_rbac_matrix(label, actor, url_key, method, payload, expected, seeded_rls_db):
    world = _make_world()
    client = APIClient()
    if actor is not None and actor != "badpw":
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {world['tokens'][actor]}")

    if url_key == "deactivate-worker":
        url = reverse(URL_NAMES[url_key], args=[world["worker_member"].id])
    else:
        url = reverse(URL_NAMES[url_key])

    body = payload(world)
    if actor == "badpw":
        response = client.post(url, {"username": "mx-owner", "password": "wrong"}, format="json")
    else:
        response = getattr(client, method.lower())(url, body, format="json")
    assert response.status_code == expected, (label, response.status_code, getattr(response, "data", ""))
