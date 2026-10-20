"""Task 2.8 / finding D5 — the saloon sensor-slot writer that never existed.

The Actor Use-Case & Dependency Map names three writers for
`saloon_sensor_slots` (Hardware Team, Device Installer via provisioning sync,
Farm Manager pre-registration) and none of them existed in code:
`provisioning_sync` only *validates* existing slots. These tests pin the new
`POST /saloons/{id}/sensor-slots/`.
"""

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.devices.models import SaloonSensorSlot
from apps.farms.api import saloon_sensor_slot_create
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization

pytestmark = pytest.mark.django_db

SLOTS = "/api/v1/saloons/{saloon}/sensor-slots/"


def _world(django_user_model):
    province = Province.objects.create(name="P-SS")
    city = City.objects.create(province=province, name="C-SS")
    org = Organization.objects.create(name="ss-org")
    farm = Farm.objects.create(org=org, name="ss-farm", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="ss-saloon")
    position = {"pk": 1}
    from apps.devices.models import SaloonPosition

    position["row"] = SaloonPosition.objects.create(
        pk=1, saloon=saloon, org=org, label="north"
    )
    return {"org": org, "farm": farm, "saloon": saloon, "position": position}


def _post(user, saloon, data, org_id=None):
    request = APIRequestFactory().post(
        SLOTS.format(saloon=saloon), data=data, format="json"
    )
    force_authenticate(request, user=user)
    request.jwt_claims = {"org_id": str(org_id)} if org_id else {}
    return saloon_sensor_slot_create(request, saloon_id=saloon)


def test_the_writer_now_exists(django_user_model):
    """The literal gap: a saloon could never be given a sensor slot over the API."""
    w = _world(django_user_model)
    member = django_user_model.objects.create_user(username="ss-manager", password="x")

    response = _post(member, w["saloon"].pk, {
        "slot_id": "SS-SALOON1-01",
        "position_id": w["position"]["row"].pk,
        "allowed_metric_keys": ["temp", "humidity"],
    }, org_id=w["org"].org_id)

    assert response.status_code == 201, response.data
    row = SaloonSensorSlot.objects.get(pk="SS-SALOON1-01")
    assert row.saloon_id == w["saloon"].pk
    assert row.org_id == w["org"].org_id
    assert row.position_id == w["position"]["row"].pk
    assert row.allowed_metric_keys == ["temp", "humidity"]


def test_slot_id_is_generated_when_omitted(django_user_model):
    w = _world(django_user_model)
    member = django_user_model.objects.create_user(username="ss-gen", password="x")
    response = _post(member, w["saloon"].pk,
                     {"position_id": w["position"]["row"].pk}, org_id=w["org"].org_id)
    assert response.status_code == 201
    assert response.data["slot_id"].startswith("SS-")


def test_recreating_the_same_slot_is_idempotent_not_a_500(django_user_model):
    """A retry on a flaky connection must not explode on the primary key."""
    w = _world(django_user_model)
    member = django_user_model.objects.create_user(username="ss-idem", password="x")
    payload = {"slot_id": "SS-IDEM", "position_id": w["position"]["row"].pk}

    assert _post(member, w["saloon"].pk, payload, w["org"].org_id).status_code == 201
    again = _post(member, w["saloon"].pk, payload, w["org"].org_id)
    assert again.status_code == 200
    assert SaloonSensorSlot.objects.filter(pk="SS-IDEM").count() == 1


def test_the_same_slot_id_in_another_saloon_is_409(django_user_model):
    w = _world(django_user_model)
    member = django_user_model.objects.create_user(username="ss-clash", password="x")
    from apps.devices.models import SaloonPosition

    other = Saloon.objects.create(org=w["org"], farm=w["farm"], name="ss-saloon-2")
    other_position = SaloonPosition.objects.create(pk=2, saloon=other, org=w["org"], label="south")

    assert _post(member, w["saloon"].pk,
                 {"slot_id": "SS-CLASH", "position_id": w["position"]["row"].pk},
                 w["org"].org_id).status_code == 201
    clash = _post(member, other.pk,
                  {"slot_id": "SS-CLASH", "position_id": other_position.pk},
                  w["org"].org_id)
    assert clash.status_code == 409
    assert clash.data["saloon_id"] == w["saloon"].pk


def test_a_foreign_saloon_is_404_not_403(django_user_model):
    """Same verdict as the other cross-org reads — no existence oracle."""
    w = _world(django_user_model)
    member = django_user_model.objects.create_user(username="ss-cross", password="x")
    other_org = Organization.objects.create(name="ss-other-org")
    other_farm = Farm.objects.create(
        org=other_org, name="ss-other", province=w["farm"].province, city=w["farm"].city
    )
    other_saloon = Saloon.objects.create(org=other_org, farm=other_farm, name="ss-other")

    response = _post(member, other_saloon.pk,
                     {"position_id": w["position"]["row"].pk}, org_id=w["org"].org_id)
    assert response.status_code == 404


def test_position_must_belong_to_the_saloon(django_user_model):
    w = _world(django_user_model)
    member = django_user_model.objects.create_user(username="ss-pos", password="x")
    from apps.devices.models import SaloonPosition

    other = Saloon.objects.create(org=w["org"], farm=w["farm"], name="ss-saloon-2")
    foreign = SaloonPosition.objects.create(pk=9, saloon=other, org=w["org"], label="other")

    response = _post(member, w["saloon"].pk,
                     {"position_id": foreign.pk}, org_id=w["org"].org_id)
    assert response.status_code == 400


def test_no_org_context_is_400(django_user_model):
    w = _world(django_user_model)
    member = django_user_model.objects.create_user(username="ss-noorg", password="x")
    response = _post(member, w["saloon"].pk, {"position_id": 1})
    assert response.status_code == 400


def test_metric_keys_must_be_strings(django_user_model):
    w = _world(django_user_model)
    member = django_user_model.objects.create_user(username="ss-metrics", password="x")
    response = _post(member, w["saloon"].pk, {
        "position_id": w["position"]["row"].pk, "allowed_metric_keys": [1, 2],
    }, org_id=w["org"].org_id)
    assert response.status_code == 400


def test_provisioning_sync_accepts_a_slot_created_this_way(django_user_model):
    """The gap's other half: sync only VALIDATED slots, it could never see one
    created here. Now the documented writer and the validator agree."""
    w = _world(django_user_model)
    member = django_user_model.objects.create_user(username="ss-sync", password="x")
    _post(member, w["saloon"].pk,
          {"slot_id": "SS-FOR-SYNC", "position_id": w["position"]["row"].pk},
          org_id=w["org"].org_id)

    # the same queryset apps/provisioning/api.py:705-709 validates against
    assert SaloonSensorSlot.objects.filter(
        org_id=w["org"].org_id, saloon_id=w["saloon"].pk, pk="SS-FOR-SYNC"
    ).exists()
