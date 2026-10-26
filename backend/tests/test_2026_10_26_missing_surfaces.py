"""The two write surfaces added 2026-10-26 — gaps the pass-2 sweep found.

Both are *missing capabilities*, not broken ones: the data model and the
readers were already there, nothing could write them.

F7b  PATCH|DELETE /saloons/{id}/sensor-slots/{slot_id}/
     `saloon_sensor_slots.is_active` had NO writer anywhere in the product:
     the LOS watchdog read it, the OpenAPI documented it, and an operator who
     pulled a sensor had no supported way to say so — so the saloon stayed
     flagged "lost" forever (the D20 orphan family, from the other side).

O17  DELETE /threshold-templates/{id}/
     There was no DELETE at all, so every template an org ever created was
     permanent — including the QA ones each sweep leaves behind. Deletion is
     refused (409) while anything still depends on the template, because a
     silent band change is the failure this endpoint must not have.
"""

import pytest
from django.urls import reverse

from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule
from apps.auditing.models import AccountActivityLog
from apps.devices.models import SaloonPosition, SaloonSensorSlot

from tests.test_m6_11_template_writes import _as, _draft, _make_world


def _slot(world, saloon=None, *, active=True, metrics=("temp", "humidity")):
    saloon = saloon or world["saloon"]
    position = SaloonPosition.objects.create(
        org=world["org"], saloon=saloon, label="north"
    )
    return SaloonSensorSlot.objects.create(
        slot_id=f"SS-TEST-{saloon.pk}",
        saloon=saloon,
        org=world["org"],
        position=position,
        is_active=active,
        allowed_metric_keys=list(metrics),
    )


def _slot_url(saloon, slot):
    return reverse("farms:saloon-sensor-slot-detail", args=[saloon.pk, slot.slot_id])


# ---------------------------------------------------------------------------
# F7b — the sensor slot write surface
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_retires_the_slot_and_audits_it():
    w = _make_world()
    slot = _slot(w)

    res = _as(w, "manager").patch(_slot_url(w["saloon"], slot), {"is_active": False}, format="json")

    assert res.status_code == 200, res.content
    assert res.data["is_active"] is False
    slot.refresh_from_db()
    assert slot.is_active is False
    assert AccountActivityLog.objects.filter(
        action="SENSOR_SLOT_UPDATED", org=w["org"], saloon=w["saloon"]
    ).exists()


@pytest.mark.django_db
def test_delete_is_the_soft_off_and_is_idempotent():
    w = _make_world()
    slot = _slot(w)
    url = _slot_url(w["saloon"], slot)

    first = _as(w, "manager").delete(url)
    assert first.status_code == 200, first.content
    assert first.data["retired"] is True
    assert "idempotent" not in first.data
    slot.refresh_from_db()
    assert slot.is_active is False
    # The row is NOT deleted: hardware history and telemetry point at it.
    assert SaloonSensorSlot.objects.filter(pk=slot.slot_id).exists()

    second = _as(w, "manager").delete(url)
    assert second.status_code == 200, second.content
    assert second.data["idempotent"] is True
    assert AccountActivityLog.objects.filter(action="SENSOR_SLOT_RETIRED").count() == 1


@pytest.mark.django_db
def test_rearming_the_slot_works():
    w = _make_world()
    slot = _slot(w, active=False)

    res = _as(w, "manager").patch(_slot_url(w["saloon"], slot), {"is_active": True}, format="json")

    assert res.status_code == 200, res.content
    slot.refresh_from_db()
    assert slot.is_active is True


@pytest.mark.django_db
def test_rescoping_metrics_replaces_the_list():
    w = _make_world()
    slot = _slot(w, metrics=("temp", "humidity"))

    res = _as(w, "manager").patch(
        _slot_url(w["saloon"], slot), {"allowed_metric_keys": ["temp"]}, format="json"
    )

    assert res.status_code == 200, res.content
    slot.refresh_from_db()
    assert slot.allowed_metric_keys == ["temp"]


@pytest.mark.django_db
def test_empty_patch_is_a_400_not_a_silent_noop():
    w = _make_world()
    slot = _slot(w)

    res = _as(w, "manager").patch(_slot_url(w["saloon"], slot), {}, format="json")

    assert res.status_code == 400
    assert "nothing to update" in res.data["error"]


@pytest.mark.django_db
def test_worker_cannot_touch_the_slot():
    w = _make_world()
    slot = _slot(w)
    url = _slot_url(w["saloon"], slot)

    assert _as(w, "worker").patch(url, {"is_active": False}, format="json").status_code == 403
    assert _as(w, "worker").delete(url).status_code == 403
    slot.refresh_from_db()
    assert slot.is_active is True


@pytest.mark.django_db
def test_a_slot_reached_through_another_saloon_is_404():
    """Slot ids are a global PK — addressing one through the wrong saloon must
    be the same 404 as an unknown id (no existence oracle)."""
    w = _make_world()
    slot = _slot(w)

    res = _as(w, "manager").patch(
        _slot_url(w["saloon_b"], slot), {"is_active": False}, format="json"
    )

    assert res.status_code == 404
    slot.refresh_from_db()
    assert slot.is_active is True


# ---------------------------------------------------------------------------
# O17 — deleting a template
# ---------------------------------------------------------------------------


def _template_url(template):
    return reverse("ops:threshold-template-update", args=[template.template_id])


@pytest.mark.django_db
def test_deleting_a_draft_removes_it_and_its_rules():
    w = _make_world()
    draft = _draft(w)
    rules = LifecycleThresholdRule.objects.filter(template=draft).count()
    assert rules > 0

    res = _as(w, "manager").delete(_template_url(draft))

    assert res.status_code == 200, res.content
    assert res.data["deleted"] is True
    assert not LifecycleTemplate.objects.filter(pk=draft.template_id).exists()
    assert LifecycleThresholdRule.objects.filter(template_id=draft.template_id).count() == 0
    audit = AccountActivityLog.objects.get(action="THRESHOLD_TEMPLATE_DELETED", org=w["org"])
    assert audit.details["template_id"] == draft.template_id
    assert audit.details["rules"] == rules


@pytest.mark.django_db
def test_deleting_an_unused_published_template_sweeps_the_cache(
    django_capture_on_commit_callbacks, monkeypatch
):
    """A published non-default template is still resolvable through the
    org+breed step, so removing it must re-resolve the affected saloons."""
    from apps.alerts import tasks

    w = _make_world()
    plan = _draft(w, breed="broiler", name="Unused plan", status=LifecycleTemplate.PUBLISHED)
    calls = []
    monkeypatch.setattr(tasks.cache_daily_thresholds, "delay", lambda *a, **k: calls.append(a))

    with django_capture_on_commit_callbacks(execute=True):
        res = _as(w, "manager").delete(_template_url(plan))

    assert res.status_code == 200, res.content
    assert calls, "removing a published template must re-cache the saloons"


@pytest.mark.django_db
def test_the_org_default_cannot_be_deleted():
    w = _make_world()
    default = LifecycleTemplate.objects.get(org=w["org"], is_default=True)

    res = _as(w, "manager").delete(_template_url(default))

    assert res.status_code == 409
    assert "default" in res.data["error"]
    assert LifecycleTemplate.objects.filter(pk=default.template_id).exists()


@pytest.mark.django_db
def test_an_assigned_template_cannot_be_deleted():
    w = _make_world()
    plan = _draft(w, breed="broiler", name="Assigned plan", status=LifecycleTemplate.PUBLISHED)
    saloon = w["saloon_b"]
    saloon.threshold_template = plan
    saloon.save(update_fields=["threshold_template"])

    res = _as(w, "manager").delete(_template_url(plan))

    assert res.status_code == 409
    assert "assigned" in res.data["error"]
    assert LifecycleTemplate.objects.filter(pk=plan.template_id).exists()


@pytest.mark.django_db
def test_foreign_and_builtin_templates_are_404():
    w = _make_world()
    foreign = _draft(w, name="Someone else's")
    foreign.org = w["other"]
    foreign.save(update_fields=["org"])
    builtin = LifecycleTemplate.objects.create(org=None, name="Built-in", breed_type=None)

    assert _as(w, "manager").delete(_template_url(foreign)).status_code == 404
    assert _as(w, "manager").delete(_template_url(builtin)).status_code == 404
    assert LifecycleTemplate.objects.filter(pk=builtin.template_id).exists()


@pytest.mark.django_db
def test_engineer_cannot_delete_a_template():
    """Authoring is engineer work; removing is a manage action."""
    w = _make_world()
    draft = _draft(w, farm=w["farm"])
    from apps.accounts.models import AccountRoleScope

    AccountRoleScope.objects.create(
        user=w["users"]["engineer"].user, org=w["org"], role="engineer", farm=w["farm"]
    )

    res = _as(w, "engineer").delete(_template_url(draft))

    assert res.status_code == 403
    assert LifecycleTemplate.objects.filter(pk=draft.template_id).exists()
