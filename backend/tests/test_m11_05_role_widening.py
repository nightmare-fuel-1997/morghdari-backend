"""Task 2.16 — widening `saloon_create` / `position_create` for installer sessions.

Before this, both writers were manager-only. An installer is org-less platform
staff, so the ONLY thing that can authorise them to write org data is the scoped
session `install_job_session` mints (AD-14) — and its `farm_id` claim is the
whole authorisation: it names the one farm they were dispatched to.

The load-bearing assertion is the negative one: a session for farm A must NOT be
able to write into farm B. Without it the widening would be a tenant hole.
"""

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.farms.api_follow import position_create, saloon_create
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization

pytestmark = pytest.mark.django_db


def _world():
    province = Province.objects.create(name="P-RW")
    city = City.objects.create(province=province, name="C-RW")
    org = Organization.objects.create(name="rw-org")
    mine = Farm.objects.create(org=org, name="rw-mine", province=province, city=city)
    other = Farm.objects.create(org=org, name="rw-other", province=province, city=city)
    return {"org": org, "mine": mine, "other": other}


def _installer_claims(w, farm):
    return {
        "org_id": str(w["org"].org_id),
        "installer_session": True,
        "job_id": 7,
        "farm_id": farm.pk,
    }


def _manager_claims(w):
    return {"org_id": str(w["org"].org_id), "roles": ["manager"]}


def _actor(django_user_model):
    """A real user object: IsAuthenticated needs one, even though the ROLE
    decision here is made entirely from the claims."""
    return django_user_model.objects.create_user(
        username=f"rw-{django_user_model.objects.count()}", password="x"
    )


def _post(view, path, claims, data, user, **kwargs):
    request = APIRequestFactory().post(path, data=data, format="json")
    request.jwt_claims = claims
    force_authenticate(request, user=user)
    return view(request, **kwargs)


def test_manager_can_still_create_a_saloon(django_user_model):
    """The widening must not disturb the existing path."""
    w = _world()
    response = _post(saloon_create, f"/api/v1/farms/{w['mine'].pk}/saloons/",
                     _manager_claims(w), {"name": "Saloon A"}, _actor(django_user_model),
                     farm_id=w["mine"].pk)
    assert response.status_code == 201, response.data
    assert Saloon.objects.filter(farm=w["mine"], name="Saloon A").exists()


def test_installer_session_can_create_a_saloon_in_ITS_farm(django_user_model):
    """AD-14 + 2.16: the session is what authorises the write."""
    w = _world()
    response = _post(saloon_create, f"/api/v1/farms/{w['mine'].pk}/saloons/",
                     _installer_claims(w, w["mine"]), {"name": "Saloon B"},
                     _actor(django_user_model), farm_id=w["mine"].pk)
    assert response.status_code == 201, response.data
    assert Saloon.objects.filter(farm=w["mine"], name="Saloon B").exists()


def test_installer_session_CANNOT_write_another_farm(django_user_model):
    """The negative that makes the widening safe: a session is one farm, not one org."""
    w = _world()
    response = _post(saloon_create, f"/api/v1/farms/{w['other'].pk}/saloons/",
                     _installer_claims(w, w["mine"]), {"name": "Trespass"},
                     _actor(django_user_model), farm_id=w["other"].pk)
    assert response.status_code == 403
    assert not Saloon.objects.filter(name="Trespass").exists()


def test_an_installer_session_without_a_farm_claim_is_refused(django_user_model):
    """No `farm_id` must mean no access, never all access."""
    w = _world()
    claims = _installer_claims(w, w["mine"])
    claims.pop("farm_id")
    response = _post(saloon_create, f"/api/v1/farms/{w['mine'].pk}/saloons/",
                     claims, {"name": "No Scope"}, _actor(django_user_model),
                     farm_id=w["mine"].pk)
    assert response.status_code == 403


def test_a_support_session_claim_does_not_open_the_writer(django_user_model):
    """AD-14: `installer_session` and `support_session` must never merge."""
    w = _world()
    claims = _installer_claims(w, w["mine"])
    claims.pop("installer_session")
    claims["support_session"] = True
    response = _post(saloon_create, f"/api/v1/farms/{w['mine'].pk}/saloons/",
                     claims, {"name": "Support Made Me"}, _actor(django_user_model),
                     farm_id=w["mine"].pk)
    assert response.status_code == 403


def test_position_create_is_widened_the_same_way(django_user_model):
    w = _world()
    saloon = Saloon.objects.create(org=w["org"], farm=w["mine"], name="RW Saloon")

    allowed = _post(position_create, f"/api/v1/saloons/{saloon.pk}/positions/",
                    _installer_claims(w, w["mine"]), {"label": "north"},
                    _actor(django_user_model), saloon_id=saloon.pk)
    assert allowed.status_code == 201, allowed.data

    # the same session, but the saloon belongs to the OTHER farm
    other_saloon = Saloon.objects.create(org=w["org"], farm=w["other"], name="RW Other")
    denied = _post(position_create, f"/api/v1/saloons/{other_saloon.pk}/positions/",
                   _installer_claims(w, w["mine"]), {"label": "trespass"},
                   _actor(django_user_model), saloon_id=other_saloon.pk)
    assert denied.status_code == 403


def test_manager_still_creates_positions(django_user_model):
    w = _world()
    saloon = Saloon.objects.create(org=w["org"], farm=w["mine"], name="RW Saloon 2")
    response = _post(position_create, f"/api/v1/saloons/{saloon.pk}/positions/",
                     _manager_claims(w), {"label": "south"}, _actor(django_user_model),
                     saloon_id=saloon.pk)
    assert response.status_code == 201, response.data
