"""Tests for complete GatewaySlot cache invalidation and immutability invariants.

Verifies:
  1. Successful GatewaySlot.save() invalidates caches after commit.
  2. GatewaySlot.delete() invalidates caches after commit.
  3. A rolled-back transaction does not execute cache invalidation prematurely.
  4. GatewaySlot creation invalidates a potentially stale negative cache.
  5. Farm assignment changes invalidate the identity cache.
  6. Status transitions invalidate status and identity caches.
  7. Supported QuerySet.update()/bulk_update() paths cannot silently leave stale authorization caches.
  8. Unrelated fields do not cause correctness problems.
  9. org_id cannot be changed through the supported application mutation paths.
"""

import uuid
import pytest
from django.db import transaction
from django.contrib.auth import get_user_model

from apps.tenants.models import Organization
from apps.farms.models import Farm
from apps.geography.models import Province, City
from apps.devices.models import GatewaySlot
from apps.devices.cache import (
    invalidate_gateway_slot_caches,
    set_redis_client,
    get_redis_client,
)
from apps.provisioning.services import transition


class FakeRedis:
    """Hermetic Redis stand-in tracking keys and deletions."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, val, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = str(val)
        return True

    def delete(self, *keys):
        count = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                count += 1
        return count

    def exists(self, key):
        return key in self.store

    def flush(self):
        self.store.clear()


@pytest.fixture
def fake_redis():
    client = FakeRedis()
    set_redis_client(client)
    yield client
    set_redis_client(None)


@pytest.fixture
def geo_data(db):
    tag = uuid.uuid4().hex[:6]
    prov = Province.objects.create(name=f"Prov-{tag}")
    cty = City.objects.create(province=prov, name=f"City-{tag}")
    return prov, cty


@pytest.fixture
def test_setup(db, geo_data):
    prov, cty = geo_data
    org_a = Organization.objects.create(name=f"Org-A-{uuid.uuid4().hex[:6]}")
    org_b = Organization.objects.create(name=f"Org-B-{uuid.uuid4().hex[:6]}")
    farm_1 = Farm.objects.create(org=org_a, name="Farm 1", province=prov, city=cty)
    farm_2 = Farm.objects.create(org=org_a, name="Farm 2", province=prov, city=cty)
    
    User = get_user_model()
    user = User.objects.create(username=f"user_{uuid.uuid4().hex[:6]}", email="test@example.com")
    return {
        "org_a": org_a,
        "org_b": org_b,
        "farm_1": farm_1,
        "farm_2": farm_2,
        "user": user,
    }


# ===========================================================================
# 1. Successful GatewaySlot.save() invalidates caches after commit
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_01_save_invalidates_caches_after_commit(fake_redis, test_setup):
    org = test_setup["org_a"]
    farm = test_setup["farm_1"]
    slot_id = f"GW-{uuid.uuid4().hex[:8]}"

    slot = GatewaySlot.objects.create(
        slot_id=slot_id, org=org, farm=farm, provisioning_status="PROVISIONED"
    )

    # Pre-populate Redis caches
    fake_redis.set(f"gw:status:{slot_id}", "PROVISIONED")
    fake_redis.set(f"gw:ident:{org.org_id}:{slot_id}", f'{{"farm_id": {farm.pk}}}')
    fake_redis.set(f"gw:neg:{org.org_id}:{slot_id}", "1")

    with transaction.atomic():
        slot.position_label = "North Shed"
        slot.save()
        # Inside transaction before commit: in on_commit model, invalidation deferred until commit
        assert fake_redis.get(f"gw:status:{slot_id}") is not None

    # After transaction commits: on_commit executes
    assert fake_redis.get(f"gw:status:{slot_id}") is None
    assert fake_redis.get(f"gw:ident:{org.org_id}:{slot_id}") is None
    assert fake_redis.get(f"gw:neg:{org.org_id}:{slot_id}") is None


# ===========================================================================
# 2. GatewaySlot.delete() invalidates caches after commit
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_02_delete_invalidates_caches_after_commit(fake_redis, test_setup):
    org = test_setup["org_a"]
    farm = test_setup["farm_1"]
    slot_id = f"GW-{uuid.uuid4().hex[:8]}"

    slot = GatewaySlot.objects.create(
        slot_id=slot_id, org=org, farm=farm, provisioning_status="PROVISIONED"
    )

    # Pre-populate Redis caches
    fake_redis.set(f"gw:status:{slot_id}", "PROVISIONED")
    fake_redis.set(f"gw:ident:{org.org_id}:{slot_id}", f'{{"farm_id": {farm.pk}}}')
    fake_redis.set(f"gw:neg:{org.org_id}:{slot_id}", "1")

    with transaction.atomic():
        slot.delete()
        # Inside transaction before commit
        assert fake_redis.get(f"gw:status:{slot_id}") is not None

    # After transaction commits
    assert fake_redis.get(f"gw:status:{slot_id}") is None
    assert fake_redis.get(f"gw:ident:{org.org_id}:{slot_id}") is None
    assert fake_redis.get(f"gw:neg:{org.org_id}:{slot_id}") is None


# ===========================================================================
# 3. Rolled-back transaction does not execute cache invalidation prematurely
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_03_rollback_does_not_execute_cache_invalidation(fake_redis, test_setup):
    org = test_setup["org_a"]
    farm = test_setup["farm_1"]
    slot_id = f"GW-{uuid.uuid4().hex[:8]}"

    slot = GatewaySlot.objects.create(
        slot_id=slot_id, org=org, farm=farm, provisioning_status="PROVISIONED"
    )

    # Pre-populate Redis caches
    fake_redis.set(f"gw:status:{slot_id}", "PROVISIONED")
    fake_redis.set(f"gw:ident:{org.org_id}:{slot_id}", f'{{"farm_id": {farm.pk}}}')

    with pytest.raises(RuntimeError):
        with transaction.atomic():
            slot.provisioning_status = "SUSPENDED"
            slot.save()
            raise RuntimeError("Database error during request")

    # Transaction was rolled back: on_commit callbacks must NOT execute
    assert fake_redis.get(f"gw:status:{slot_id}") == "PROVISIONED"
    assert fake_redis.get(f"gw:ident:{org.org_id}:{slot_id}") == f'{{"farm_id": {farm.pk}}}'


# ===========================================================================
# 4. GatewaySlot creation invalidates a potentially stale negative cache
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_04_creation_invalidates_negative_cache(fake_redis, test_setup):
    org = test_setup["org_a"]
    farm = test_setup["farm_1"]
    slot_id = f"GW-{uuid.uuid4().hex[:8]}"

    # A rogue or probing request previously caused a negative cache entry
    fake_redis.set(f"gw:neg:{org.org_id}:{slot_id}", "1")
    assert fake_redis.get(f"gw:neg:{org.org_id}:{slot_id}") == "1"

    # Now the gateway slot is created
    with transaction.atomic():
        GatewaySlot.objects.create(
            slot_id=slot_id, org=org, farm=farm, provisioning_status="PENDING_INSTALLATION"
        )

    # After commit, negative cache is purged
    assert fake_redis.get(f"gw:neg:{org.org_id}:{slot_id}") is None


# ===========================================================================
# 5. Farm assignment changes invalidate the identity cache
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_05_farm_change_invalidates_identity_cache(fake_redis, test_setup):
    org = test_setup["org_a"]
    farm_1 = test_setup["farm_1"]
    farm_2 = test_setup["farm_2"]
    slot_id = f"GW-{uuid.uuid4().hex[:8]}"

    slot = GatewaySlot.objects.create(
        slot_id=slot_id, org=org, farm=farm_1, provisioning_status="PROVISIONED"
    )

    # 24-hour identity cache exists pointing to farm_1
    fake_redis.set(f"gw:ident:{org.org_id}:{slot_id}", f'{{"farm_id": {farm_1.pk}}}')

    with transaction.atomic():
        slot.farm = farm_2
        slot.save()

    # Identity cache holding old farm is purged upon commit
    assert fake_redis.get(f"gw:ident:{org.org_id}:{slot_id}") is None


# ===========================================================================
# 6. Status transitions invalidate status and identity caches
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_06_status_transition_invalidates_caches(fake_redis, test_setup):
    org = test_setup["org_a"]
    farm = test_setup["farm_1"]
    user = test_setup["user"]
    slot_id = f"GW-{uuid.uuid4().hex[:8]}"

    slot = GatewaySlot.objects.create(
        slot_id=slot_id, org=org, farm=farm, provisioning_status="PROVISIONED"
    )

    fake_redis.set(f"gw:status:{slot_id}", "PROVISIONED")
    fake_redis.set(f"gw:ident:{org.org_id}:{slot_id}", f'{{"farm_id": {farm.pk}}}')

    # Transition to SUSPENDED
    transition(slot_id, "SUSPENDED", user, reason="Maintenance")

    assert fake_redis.get(f"gw:status:{slot_id}") is None
    assert fake_redis.get(f"gw:ident:{org.org_id}:{slot_id}") is None


# ===========================================================================
# 7. Supported QuerySet.update()/bulk_update() paths invalidate caches
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_07_queryset_update_and_bulk_update_invalidate_caches(fake_redis, test_setup):
    org = test_setup["org_a"]
    farm_1 = test_setup["farm_1"]
    farm_2 = test_setup["farm_2"]
    slot_id1 = f"GW-{uuid.uuid4().hex[:8]}"
    slot_id2 = f"GW-{uuid.uuid4().hex[:8]}"

    slot1 = GatewaySlot.objects.create(slot_id=slot_id1, org=org, farm=farm_1, provisioning_status="PROVISIONED")
    slot2 = GatewaySlot.objects.create(slot_id=slot_id2, org=org, farm=farm_1, provisioning_status="PROVISIONED")

    # A) QuerySet.update() test
    fake_redis.set(f"gw:ident:{org.org_id}:{slot_id1}", "cached_data")
    with transaction.atomic():
        GatewaySlot.objects.filter(pk=slot_id1).update(farm=farm_2)
    assert fake_redis.get(f"gw:ident:{org.org_id}:{slot_id1}") is None

    # B) QuerySet.bulk_update() test
    fake_redis.set(f"gw:status:{slot_id1}", "PROVISIONED")
    fake_redis.set(f"gw:status:{slot_id2}", "PROVISIONED")
    slot1.provisioning_status = "SUSPENDED"
    slot2.provisioning_status = "SUSPENDED"
    with transaction.atomic():
        GatewaySlot.objects.bulk_update([slot1, slot2], fields=["provisioning_status"])
    assert fake_redis.get(f"gw:status:{slot_id1}") is None
    assert fake_redis.get(f"gw:status:{slot_id2}") is None

    # C) QuerySet.delete() bulk deletion test
    fake_redis.set(f"gw:status:{slot_id1}", "SUSPENDED")
    fake_redis.set(f"gw:status:{slot_id2}", "SUSPENDED")
    with transaction.atomic():
        GatewaySlot.objects.filter(pk__in=[slot_id1, slot_id2]).delete()
    assert fake_redis.get(f"gw:status:{slot_id1}") is None
    assert fake_redis.get(f"gw:status:{slot_id2}") is None


# ===========================================================================
# 8. Unrelated fields do not cause correctness problems
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_08_unrelated_fields_save_and_invalidate_cleanly(fake_redis, test_setup):
    org = test_setup["org_a"]
    farm = test_setup["farm_1"]
    slot_id = f"GW-{uuid.uuid4().hex[:8]}"

    slot = GatewaySlot.objects.create(
        slot_id=slot_id, org=org, farm=farm, provisioning_status="PROVISIONED"
    )

    with transaction.atomic():
        slot.desired_config_version = 42
        slot.position_label = "Sensor Section 4"
        slot.save()

    slot.refresh_from_db()
    assert slot.desired_config_version == 42
    assert slot.position_label == "Sensor Section 4"


# ===========================================================================
# 9. org_id cannot be changed through supported application mutation paths
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_09_org_id_immutability_enforced(test_setup):
    org_a = test_setup["org_a"]
    org_b = test_setup["org_b"]
    farm = test_setup["farm_1"]
    slot_id = f"GW-{uuid.uuid4().hex[:8]}"

    slot = GatewaySlot.objects.create(
        slot_id=slot_id, org=org_a, farm=farm, provisioning_status="PROVISIONED"
    )

    # 1. Model save() cannot mutate org
    slot.org = org_b
    with pytest.raises(ValueError, match="org_id is immutable"):
        slot.save()

    # 2. QuerySet.update() cannot mutate org
    with pytest.raises(ValueError, match="org_id is immutable"):
        GatewaySlot.objects.filter(pk=slot_id).update(org=org_b)

    with pytest.raises(ValueError, match="org_id is immutable"):
        GatewaySlot.objects.filter(pk=slot_id).update(org_id=org_b.pk)

    # 3. QuerySet.bulk_update() cannot mutate org
    slot.refresh_from_db()
    assert slot.org == org_a
    slot.org = org_b
    with pytest.raises(ValueError, match="org_id is immutable"):
        GatewaySlot.objects.bulk_update([slot], fields=["org"])
