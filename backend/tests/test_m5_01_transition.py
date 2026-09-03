import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.tenants.models import Organization

from apps.devices.models import GatewaySlot
from apps.provisioning.models import ProvisioningAuditLog
from apps.provisioning.services import STATUS_CACHE_KEY, cached_status, transition


@pytest.mark.django_db
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_transition_writes_audit_updates_status_and_invalidates_cache(
    django_user_model
):
    organization = Organization.objects.create(name="transition-org")
    user = django_user_model.objects.create_user(username="installer", password="x")
    slot = GatewaySlot.objects.create(
        slot_id="GW-M5-01",
        org=organization,
        created_by=user,
        provisioning_status="PENDING_INSTALLATION",
    )
    key = STATUS_CACHE_KEY.format(slot_id=slot.slot_id)
    cache.set(key, slot.provisioning_status)

    transition(slot.slot_id, "PENDING_APPROVAL", user, reason="sync submitted")

    slot.refresh_from_db()
    audit = ProvisioningAuditLog.objects.get(gateway_slot=slot)
    assert slot.provisioning_status == "PENDING_APPROVAL"
    assert audit.previous_status == "PENDING_INSTALLATION"
    assert audit.new_status == "PENDING_APPROVAL"
    assert audit.reason == "sync submitted"
    assert cache.get(key) is None


@pytest.mark.django_db
def test_transition_is_idempotent_for_same_status(django_user_model):
    organization = Organization.objects.create(name="transition-org-2")
    user = django_user_model.objects.create_user(username="installer2", password="x")
    slot = GatewaySlot.objects.create(
        slot_id="GW-M5-02", org=organization, created_by=user
    )

    transition(slot.slot_id, "PENDING_INSTALLATION", user)

    assert ProvisioningAuditLog.objects.filter(gateway_slot=slot).count() == 0


@pytest.mark.django_db
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_cached_status_populates_and_returns_status(django_user_model):
    organization = Organization.objects.create(name="transition-org-3")
    user = django_user_model.objects.create_user(username="installer3", password="x")
    slot = GatewaySlot.objects.create(
        slot_id="GW-M5-03", org=organization, created_by=user
    )

    assert cached_status(slot.slot_id) == "PENDING_INSTALLATION"
    assert cached_status(slot.slot_id) == "PENDING_INSTALLATION"
