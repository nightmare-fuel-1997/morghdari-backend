"""Phase 2 slice (b) — assignment, the installer queue, and the job session.

The load-bearing assertions are the RLS ones: an org-less installer has no GUC,
so a naive queryset would return zero rows and the queue would silently lie.
Everything here is built on the admin-alias discovery pattern the subscriber
already uses.
"""

import pytest
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import AccountRoleScope, OrgMember
from apps.auditing.models import AccountActivityLog
from apps.provisioning.models import InstallationJob
from apps.provisioning.installations import (
    install_assignable,
    provisioning_config_state,
    install_job_bind,
    install_job_bind_override,
    install_job_sensor_map,
    install_sensor_photo,
    install_job_submit,
    install_gateway_photo,
    _store_device_photo,
    install_job_assign,
    install_job_detail,
    install_job_session,
    install_jobs,
)
from apps.auditing.models import PlatformAuditLog
from apps.geography.models import City, Province
from apps.tenants.models import Organization
from apps.farms.models import Farm

pytestmark = pytest.mark.django_db

JOBS = "/api/v1/provisioning/installations/"


def _staff(django_user_model, username, role):
    user = django_user_model.objects.create_user(username=username, password="x")
    user.is_platform_staff = True
    user.staff_role = role
    user.save(update_fields=["is_platform_staff", "staff_role"])
    return user


def _plain(django_user_model, username):
    return django_user_model.objects.create_user(username=username, password="x")


def _installer(django_user_model, username, org):
    """A Device Installer: platform staff of the software company, org-less,
    listed as an active installer member of the org whose jobs they will do."""
    user = _staff(django_user_model, username, "installer")
    OrgMember.objects.create(user=user, org=org, role="installer", is_active=True)
    return user


def _world(django_user_model, gateways=2):
    province = Province.objects.create(name="P-Job")
    city = City.objects.create(province=province, name="C-Job")
    org = Organization.objects.create(name="job-org")
    farm = Farm.objects.create(org=org, name="job-farm", province=province, city=city)
    support = _staff(django_user_model, "job-support", "support")
    jobs = InstallationJob.objects.bulk_create(
        [
            InstallationJob(
                org=org, farm=farm, kind="INSTALL", state="PENDING",
                request_id=None,
            )
            for _ in range(gateways)
        ]
    )
    return {"province": province, "city": city, "org": org, "farm": farm,
            "support": support, "jobs": jobs}


def _call(view, method, path, user, data=None, **kwargs):
    request = getattr(APIRequestFactory(), method.lower())(path, data=data or {}, format="json")
    force_authenticate(request, user=user)
    return view(request, **kwargs)


def _call_job(view, method, path, user, data=None, job_id=None):
    request = getattr(APIRequestFactory(), method.lower())(path, data=data or {}, format="json")
    force_authenticate(request, user=user)
    return view(request, job_id=job_id)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", [None, "manager"])
def test_non_platform_staff_cannot_see_the_queue(django_user_model, role):
    w = _world(django_user_model)
    user = _plain(django_user_model, "job-plain")
    if role:
        OrgMember.objects.create(user=user, org=w["org"], role=role, is_active=True)
    response = _call(install_jobs, "GET", JOBS, user)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# assignment
# ---------------------------------------------------------------------------

def test_support_assigns_an_active_installer(django_user_model):
    w = _world(django_user_model)
    installer = _plain(django_user_model, "job-installer")
    OrgMember.objects.create(user=installer, org=w["org"], role="installer", is_active=True)

    response = _call_job(
        install_job_assign, "POST",
        JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=w["jobs"][0].pk,
    )

    assert response.status_code == 200
    job = InstallationJob.objects.get(pk=w["jobs"][0].pk)
    assert job.assigned_installer_id == installer.pk
    assert job.assigned_by_id == w["support"].pk
    assert job.assigned_at is not None
    assert job.state == "ASSIGNED"

    audit = PlatformAuditLog.objects.get(target_id=str(job.pk))
    assert audit.action == "PLATFORM_JOB_ASSIGN"
    assert audit.actor_id == w["support"].pk
    assert audit.details["installer_id"] == installer.pk
    # the org-side trail too, for the tenant's own querying
    assert AccountActivityLog.objects.filter(action="INSTALLER_JOB_ASSIGN", org=w["org"]).exists()


def test_assigning_a_non_member_is_400(django_user_model):
    w = _world(django_user_model)
    stranger = _plain(django_user_model, "job-stranger")
    response = _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": stranger.pk}, job_id=w["jobs"][0].pk,
    )
    assert response.status_code == 400
    assert InstallationJob.objects.get(pk=w["jobs"][0].pk).assigned_installer_id is None


def test_reassignment_after_bind_is_flagged_not_silent(django_user_model):
    """AD-21: allowed, but visible in the job history."""
    w = _world(django_user_model)
    first = _plain(django_user_model, "job-first")
    second = _plain(django_user_model, "job-second")
    for user in (first, second):
        OrgMember.objects.create(user=user, org=w["org"], role="installer", is_active=True)
    InstallationJob.objects.filter(pk=w["jobs"][0].pk).update(state="BOUND")

    _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": first.pk}, job_id=w["jobs"][0].pk,
    )
    response = _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": second.pk}, job_id=w["jobs"][0].pk,
    )

    assert response.status_code == 200
    job = InstallationJob.objects.get(pk=w["jobs"][0].pk)
    assert job.assigned_installer_id == second.pk
    assert job.reassigned_after_bind is True
    assert job.state == "BOUND"  # not dragged back to ASSIGNED


def test_completed_job_cannot_be_reassigned(django_user_model):
    w = _world(django_user_model)
    installer = _plain(django_user_model, "job-done-installer")
    OrgMember.objects.create(user=installer, org=w["org"], role="installer", is_active=True)
    InstallationJob.objects.filter(pk=w["jobs"][0].pk).update(state="COMPLETED")
    response = _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=w["jobs"][0].pk,
    )
    assert response.status_code == 409


def test_installer_cannot_assign_to_themselves(django_user_model):
    """Someone else must dispatch; otherwise the queue is theatre."""
    w = _world(django_user_model)
    installer = _plain(django_user_model, "job-self")
    OrgMember.objects.create(user=installer, org=w["org"], role="installer", is_active=True)
    response = _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", installer,
        {"installer_id": installer.pk}, job_id=w["jobs"][0].pk,
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# the assignable picker
# ---------------------------------------------------------------------------

def test_assignable_respects_regional_scope(django_user_model):
    """account_role_scopes province scoping (Tracker §4i)."""
    w = _world(django_user_model)
    other_province = Province.objects.create(name="P-Other")
    free = _installer(django_user_model, "job-free", w["org"])
    local = _installer(django_user_model, "job-local", w["org"])
    far = _installer(django_user_model, "job-far", w["org"])
    # memberships already created by _installer()
    AccountRoleScope.objects.create(org=w["org"], user=local, role="installer", province=w["province"])
    AccountRoleScope.objects.create(org=w["org"], user=far, role="installer", province=other_province)

    response = _call(
        install_assignable, "GET",
        JOBS + f"assignable/?org_id={w['org'].org_id}&province_id={w['province'].pk}",
        w["support"], {},
    )
    assert response.status_code == 200
    usernames = {row["username"] for row in response.data["results"]}
    # unscoped sees everything provincial-agnostic; the far installer is excluded
    assert usernames == {"job-free", "job-local"}


def test_assignable_lists_everyone_without_a_province_filter(django_user_model):
    w = _world(django_user_model)
    a = _installer(django_user_model, "job-a", w["org"])
    b = _installer(django_user_model, "job-b", w["org"])
    # memberships already created by _installer()
    response = _call(
        install_assignable, "GET", JOBS + f"assignable/?org_id={w['org'].org_id}", w["support"], {},
    )
    assert {row["username"] for row in response.data["results"]} == {"job-a", "job-b"}


# ---------------------------------------------------------------------------
# the queue — the RLS load-bearing part
# ---------------------------------------------------------------------------

def test_orgless_installer_sees_only_their_own_jobs(django_user_model):
    """Without admin-alias discovery this returns zero rows and the queue lies."""
    w = _world(django_user_model)
    mine = _installer(django_user_model, "job-mine", w["org"])
    theirs = _installer(django_user_model, "job-theirs", w["org"])
    # memberships already created by _installer()
    InstallationJob.objects.filter(pk=w["jobs"][0].pk).update(assigned_installer=mine)
    InstallationJob.objects.filter(pk=w["jobs"][1].pk).update(assigned_installer=theirs)

    response = _call(install_jobs, "GET", JOBS, mine)
    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["id"] == w["jobs"][0].pk


def test_support_sees_every_job(django_user_model):
    w = _world(django_user_model, gateways=3)
    response = _call(install_jobs, "GET", JOBS, w["support"])
    assert response.data["count"] == 3


def test_state_filter_narrows_the_queue(django_user_model):
    w = _world(django_user_model, gateways=3)
    InstallationJob.objects.filter(pk__in=[w["jobs"][0].pk, w["jobs"][1].pk]).update(state="BOUND")
    response = _call(install_jobs, "GET", JOBS + "?state=BOUND", w["support"])
    assert response.data["count"] == 2
    bad = _call(install_jobs, "GET", JOBS + "?state=NOWHERE", w["support"])
    assert bad.status_code == 400


def test_job_detail_carries_the_derived_gateway_state(django_user_model):
    """AD-22 — derived, never stored."""
    w = _world(django_user_model)
    from apps.devices.models import DeviceUnit, GatewaySlot

    job = w["jobs"][0]
    InstallationJob.objects.filter(pk=job.pk).update(state="BOUND")
    DeviceUnit.objects.create(unit_id="GW-QUEUE-1", hardware_id="HW-QUEUE-1", state="BOUND")
    slot = GatewaySlot.objects.create(
        slot_id="GW-QUEUE-1", org=w["org"], farm=w["farm"],
        provisioning_status="PENDING_INSTALLATION",
        desired_config_version=1, applied_config_version=0,
    )
    InstallationJob.objects.filter(pk=job.pk).update(unit_id="GW-QUEUE-1", gateway_slot=slot)

    response = _call_job(install_job_detail, "GET", JOBS + f"{job.pk}/", w["support"], job_id=job.pk)
    assert response.status_code == 200
    body = response.data
    assert body["gateway_slot_id"] == "GW-QUEUE-1"
    assert body["gateway_state_view"] == "STALLED"  # desired 1 > applied 0
    assert body["unit_id"] == "GW-QUEUE-1"


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------

def test_session_scopes_the_installer_to_one_org(django_user_model):
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-sess", w["org"])
    _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=w["jobs"][0].pk,
    )

    response = _call_job(
        install_job_session, "POST", JOBS + f"{w['jobs'][0].pk}/session/", installer,
        job_id=w["jobs"][0].pk,
    )
    assert response.status_code == 200
    body = response.data

    from rest_framework_simplejwt.tokens import AccessToken

    claims = AccessToken(body["access_token"])
    assert claims["installer_session"] is True
    assert "support_session" not in claims  # AD-14 — the two must never merge
    assert claims["org_id"] == str(w["org"].org_id)
    assert claims["job_id"] == w["jobs"][0].pk
    assert claims["farm_id"] == w["farm"].pk
    assert body["expires_in_sec"] == 900

    log = AccountActivityLog.objects.get(action="INSTALLER_SESSION_OPEN")
    assert log.org_id == w["org"].org_id
    assert log.details["installer_id"] == installer.pk


def test_unassigned_job_has_no_session(django_user_model):
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-unassigned", w["org"])
    response = _call_job(
        install_job_session, "POST", JOBS + f"{w['jobs'][0].pk}/session/", installer,
        job_id=w["jobs"][0].pk,
    )
    assert response.status_code == 409


def test_other_installer_cannot_open_the_session(django_user_model):
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-owner", w["org"])
    other = _installer(django_user_model, "job-other", w["org"])
    # memberships already created by _installer()
    _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=w["jobs"][0].pk,
    )
    response = _call_job(
        install_job_session, "POST", JOBS + f"{w['jobs'][0].pk}/session/", other,
        job_id=w["jobs"][0].pk,
    )
    assert response.status_code == 403


def test_support_can_open_a_session_for_a_broken_assignee(django_user_model):
    """Support may step in for an installer whose phone died — but never silently.

    The session is still scoped to the JOB and still audited under the org, so a
    support walkthrough leaves the same trail as the installer's own. The token
    carries `installer_session`, never `support_session`: it grants NO support
    operator's powers, only the same org context the installer had.
    """
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-support-sess", w["org"])
    _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=w["jobs"][0].pk,
    )
    response = _call_job(
        install_job_session, "POST", JOBS + f"{w['jobs'][0].pk}/session/", w["support"],
        job_id=w["jobs"][0].pk,
    )
    assert response.status_code == 200
    from rest_framework_simplejwt.tokens import AccessToken

    claims = AccessToken(response.data["access_token"])
    assert claims["installer_session"] is True
    assert "support_session" not in claims
    assert claims["job_id"] == w["jobs"][0].pk
    log = AccountActivityLog.objects.get(action="INSTALLER_SESSION_OPEN")
    assert log.details["installer_id"] == w["support"].pk


# ---------------------------------------------------------------------------
# the bind (slice c)
# ---------------------------------------------------------------------------

def _unit(django_user_model, org, unit_id, claim_code="claim-secret-1"):
    """A factory unit exactly as `POST /platform/devices/units/` leaves it.

    The credential and the 3 device-lane ACL rows matter: the bind must EXPAND
    that set to the tenant topics, not replace it, and only a fixture that
    creates them can prove it.
    """
    from apps.devices.models import DeviceUnit
    from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
    from apps.mqtt_auth.services import bootstrap_acl_rule_items, hash_password

    creator = _plain(django_user_model, f"creator-{unit_id}")
    unit = DeviceUnit.objects.create(
        unit_id=unit_id, hardware_id=f"HW-{unit_id}",
        claim_code_hash=hash_password(claim_code), state="REGISTERED",
        created_by=creator,
    )
    MqttDeviceCredential.objects.create(
        device_id=unit_id, username=unit_id,
        password_hash=hash_password("broker-pass-1"), is_active=True,
    )
    for item in bootstrap_acl_rule_items(unit_id):
        MqttAclRule.objects.create(
            username=unit_id, topic=item["topic"],
            permission=item["permission"], action=item["action"],
        )
    return unit


def _bind(view, job_id, user, payload, job=None):
    return _call_job(
        view, "POST", JOBS + f"{job_id}/gateway/", user, payload, job_id=job_id
    )


@pytest.mark.django_db(transaction=True)
def test_bind_joins_the_factory_unit_to_the_job(django_user_model):
    """The moment the org-less unit and the org-scoped job become one thing."""
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-bind-1", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    unit = _unit(django_user_model, w["org"], "GW-BIND-0001")

    response = _bind(install_job_bind, job.pk, installer,
                         {"unit_id": unit.unit_id, "claim_code": "claim-secret-1"})

    assert response.status_code == 200, response.data
    body = response.data["job"]
    assert body["state"] == "BOUND"
    assert body["unit_id"] == "GW-BIND-0001"
    assert body["gateway_slot_id"] == "GW-BIND-0001"  # AD-2: one identity

    unit.refresh_from_db()
    assert unit.state == "BOUND"
    assert unit.claimed_at is not None
    assert unit.slot_id == "GW-BIND-0001"

    from apps.devices.models import GatewaySlot

    slot = GatewaySlot.objects.get(pk="GW-BIND-0001")
    assert slot.org_id == w["org"].org_id
    assert slot.farm_id == w["farm"].pk
    assert slot.desired_config_version == 1  # AD-4: bumped, so the doc is NEW

    # the ACL EXPANDED — 3 device-lane rows survive, the tenant set is added
    from apps.mqtt_auth.models import MqttAclRule

    rules = MqttAclRule.objects.filter(username="GW-BIND-0001")
    topics = {r.topic for r in rules}
    assert f"devices/GW-BIND-0001/desired" in topics, "the device lane was dropped"
    assert f"devices/GW-BIND-0001/reported" in topics
    assert f"{w['org'].org_id}/farms/{w['farm'].pk}/gateways/GW-BIND-0001/telemetry" in topics
    assert rules.count() == 13  # 4 kept + 9 tenant (command/response since 2026-10-20)

    # a PROVISION command was queued for the device
    from apps.provisioning.models import GatewayCommand

    command = GatewayCommand.objects.get(gateway_slot_id="GW-BIND-0001")
    assert command.kind == "PROVISION"
    assert command.state == "PENDING"
    assert command.payload["config_version"] == 1


def test_bind_consumes_the_claim_code_once(django_user_model):
    """Single use, enforced by the stamp rather than by politeness."""
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-bind-2", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    unit = _unit(django_user_model, w["org"], "GW-BIND-0002")

    assert _bind(install_job_bind, job.pk, installer,
                 {"unit_id": unit.unit_id, "claim_code": "claim-secret-1"}).status_code == 200

    # the same installer, second press — a replay, not a new bind
    replay = _bind(install_job_bind, job.pk, installer,
                   {"unit_id": unit.unit_id, "claim_code": "claim-secret-1"})
    assert replay.status_code == 200
    assert replay.data["already_bound"] is True

    # a DIFFERENT installer with the same code is refused
    other = _installer(django_user_model, "job-bind-3", w["org"])
    _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][1].pk}/assign/", w["support"],
        {"installer_id": other.pk}, job_id=w["jobs"][1].pk,
    )
    assert _bind(install_job_bind, w["jobs"][1].pk, other,
                 {"unit_id": unit.unit_id, "claim_code": "claim-secret-1"}).status_code == 409


def test_wrong_claim_code_is_400(django_user_model):
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-bind-4", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    _unit(django_user_model, w["org"], "GW-BIND-0004")
    response = _bind(install_job_bind, job.pk, installer,
                     {"unit_id": "GW-BIND-0004", "claim_code": "wrong"})
    assert response.status_code == 400
    from apps.devices.models import DeviceUnit

    assert DeviceUnit.objects.get(pk="GW-BIND-0004").claimed_at is None


@pytest.mark.django_db(transaction=True)
def test_a_bound_unit_cannot_be_bound_to_a_second_job(django_user_model):
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-bind-5", w["org"])
    first, second = w["jobs"][0], w["jobs"][1]
    _call_job(
        install_job_assign, "POST", JOBS + f"{first.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=first.pk,
    )
    _call_job(
        install_job_assign, "POST", JOBS + f"{second.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=second.pk,
    )
    _unit(django_user_model, w["org"], "GW-BIND-0005")

    assert _bind(install_job_bind, first.pk, installer,
                     {"unit_id": "GW-BIND-0005", "claim_code": "claim-secret-1"}).status_code == 200

    # the override path bypasses the code but not the single-binding rule
    response = _bind(install_job_bind_override, second.pk, w["support"],
                     {"unit_id": "GW-BIND-0005", "reason": "label torn"})
    assert response.status_code == 409


@pytest.mark.django_db(transaction=True)
def test_bind_works_with_the_unit_offline(django_user_model):
    """No broker call at all: the publish is deferred to on_commit and swallowed.

    This is the property that makes a farm installable when the device is still
    in its box — we assert by NOT executing the on_commit callbacks, which is
    exactly the "broker unreachable" shape.
    """
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-bind-6", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    _unit(django_user_model, w["org"], "GW-BIND-0006")

    response = _bind(install_job_bind, job.pk, installer,
                         {"unit_id": "GW-BIND-0006", "claim_code": "claim-secret-1"})

    assert response.status_code == 200
    job.refresh_from_db()
    assert job.state == "BOUND"
    # the command is still queued, so the convergence sweep will retry
    from apps.provisioning.models import GatewayCommand

    assert GatewayCommand.objects.filter(gateway_slot_id="GW-BIND-0006").exists()


@pytest.mark.django_db(transaction=True)
def test_override_bind_requires_a_reason_and_audits_it(django_user_model):
    w = _world(django_user_model)
    job = w["jobs"][0]
    _unit(django_user_model, w["org"], "GW-BIND-0007")

    missing = _bind(install_job_bind_override, job.pk, w["support"],
                    {"unit_id": "GW-BIND-0007"})
    assert missing.status_code == 400
    assert "reason" in str(missing.data)

    ok = _bind(install_job_bind_override, job.pk, w["support"],
                   {"unit_id": "GW-BIND-0007", "reason": "label destroyed in transit"})
    assert ok.status_code == 200

    audit = PlatformAuditLog.objects.get(action="PLATFORM_JOB_BIND")
    assert audit.details["via_override"] is True
    assert audit.details["reason"] == "label destroyed in transit"


def test_an_installer_cannot_use_the_override_path(django_user_model):
    """The override skips the possession proof, so it is support-only."""
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-bind-8", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    _unit(django_user_model, w["org"], "GW-BIND-0008")
    response = _bind(install_job_bind_override, job.pk, installer,
                     {"unit_id": "GW-BIND-0008", "reason": "I lost the label"})
    assert response.status_code == 403


def test_a_retired_unit_is_404_at_bind(django_user_model):
    """Same verdict as unknown — the endpoint is not an inventory oracle."""
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-bind-9", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    unit = _unit(django_user_model, w["org"], "GW-BIND-0009")
    unit.state = "RETIRED"
    unit.save(update_fields=["state"])

    response = _bind(install_job_bind, job.pk, installer,
                     {"unit_id": unit.unit_id, "claim_code": "claim-secret-1"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# slices (d) submit and (e) the four-state view
# ---------------------------------------------------------------------------

def _submit(job_id, user):
    return _call_job(
        install_job_submit, "POST", JOBS + f"{job_id}/submit/", user, {}, job_id=job_id
    )


def _config_state(user, state=None):
    path = JOBS + "../config-state/"
    if state:
        path += f"?state={state}"
    return _call(provisioning_config_state, "GET", path, user)


@pytest.mark.django_db(transaction=True)
def test_submit_activates_without_human_approval(django_user_model):
    """AD-17: applied == desired AND job submitted → PROVISIONED, no manager.

    This is the whole point of removing review-approval minting, so the test
    drives the real reconciler rather than asserting on a view.
    """
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-sub-1", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    unit = _unit(django_user_model, w["org"], "GW-SUB-0001")

    response = _bind(install_job_bind, job.pk, installer,
                     {"unit_id": unit.unit_id, "claim_code": "claim-secret-1"})
    assert response.status_code == 200

    # an unbound job cannot be submitted
    unbound = w["jobs"][1]
    assert _submit(unbound.pk, installer).status_code == 409

    from apps.provisioning.tasks import reconcile_reported

    # the device reports BEFORE the installer presses Done — the realistic order
    # when the installer walks away first
    outcome = reconcile_reported(
        {"schema_version": "1.0", "slot_id": "GW-SUB-0001",
         "config_version": 1, "state": "APPLIED"},
        topic_slot_id="GW-SUB-0001",
    )
    assert outcome == "APPLIED"

    # not yet PROVISIONED: the job has not been submitted (AD-17 needs BOTH)
    from apps.devices.models import GatewaySlot

    slot = GatewaySlot.objects.get(pk="GW-SUB-0001")
    assert slot.provisioning_status == "PENDING_INSTALLATION"

    assert _submit(job.pk, installer).status_code == 200

    slot.refresh_from_db()
    assert slot.provisioning_status == "PROVISIONED"
    assert slot.applied_at is not None
    job.refresh_from_db()
    assert job.state == "COMPLETED"
    assert job.completed_at is not None


def test_submit_is_idempotent(django_user_model):
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-sub-2", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    unit = _unit(django_user_model, w["org"], "GW-SUB-0002")
    _bind(install_job_bind, job.pk, installer,
          {"unit_id": unit.unit_id, "claim_code": "claim-secret-1"})

    assert _submit(job.pk, installer).status_code == 200
    again = _submit(job.pk, installer)
    assert again.status_code == 200
    assert again.data["already_submitted"] is True
    assert PlatformAuditLog.objects.filter(action="PLATFORM_JOB_SUBMIT").count() == 1


def test_four_state_view_derives_never_stores(django_user_model):
    """AD-22 — the view is derived, so it cannot disagree with the counters."""
    w = _world(django_user_model)
    installer = _installer(django_user_model, "job-state-1", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    unit = _unit(django_user_model, w["org"], "GW-STATE-0001")

    # 1. PENDING_SYNC — a slot that has never been sent a configuration. The
    #    bind is what creates the slot, so the state before it does not exist;
    #    this is the shape a slot created by any path that does not publish.
    from apps.devices.models import GatewaySlot

    GatewaySlot.objects.create(
        slot_id="GW-STATE-0001", org=w["org"], farm=w["farm"],
        provisioning_status="PENDING_INSTALLATION", desired_config_version=0,
    )
    response = _config_state(w["support"], "PENDING_SYNC")
    assert response.status_code == 200
    assert any(row["gateway_slot_id"] == "GW-STATE-0001" for row in response.data["results"])

    _bind(install_job_bind, job.pk, installer,
          {"unit_id": unit.unit_id, "claim_code": "claim-secret-1"})

    # 2. bound, desired 1 applied 0 — STALLED
    stalled = _config_state(w["support"], "STALLED")
    assert any(row["gateway_slot_id"] == "GW-STATE-0001" for row in stalled.data["results"])

    # 3. a failed report — FAILED, and it carries the reason
    from apps.provisioning.tasks import reconcile_reported

    reconcile_reported(
        {"schema_version": "1.0", "slot_id": "GW-STATE-0001", "config_version": 1,
         "state": "FAILED", "error": "tamper switch open"},
        topic_slot_id="GW-STATE-0001",
    )
    failed = _config_state(w["support"], "FAILED")
    row = next(r for r in failed.data["results"] if r["gateway_slot_id"] == "GW-STATE-0001")
    assert row["last_publish_error"] == "tamper switch open"

    # 4. converged — READY, and nothing is STALLED any more
    reconcile_reported(
        {"schema_version": "1.0", "slot_id": "GW-STATE-0001",
         "config_version": 1, "state": "APPLIED"},
        topic_slot_id="GW-STATE-0001",
    )
    ready = _config_state(w["support"], "READY")
    assert any(r["gateway_slot_id"] == "GW-STATE-0001" for r in ready.data["results"])
    after = _config_state(w["support"], "STALLED")
    assert not any(r["gateway_slot_id"] == "GW-STATE-0001" for r in after.data["results"])


def test_config_state_gate_and_filter(django_user_model):
    w = _world(django_user_model)
    assert _config_state(_plain(django_user_model, "job-state-nobody")).status_code == 403
    assert _config_state(w["support"], "nowhere").status_code == 400


# ---------------------------------------------------------------------------
# slice f — pack → sensor-slot mapping (AD-23, task 2.10)
# ---------------------------------------------------------------------------



def _bound_with_saloon(django_user_model, tag):
    """A job whose gateway is bound, with a saloon + position + sensor slot.

    `SaloonSensorSlot` needs a position (FK, RESTRICT), which needs a saloon —
    the real chain an installer follows, so the tests build it rather than
    stubbing it away.
    """
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Saloon

    w = _world(django_user_model)
    installer = _installer(django_user_model, f"map-{tag}", w["org"])
    job = w["jobs"][0]
    _call_job(
        install_job_assign, "POST", JOBS + f"{job.pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=job.pk,
    )
    unit_id = f"GW-MAP-{tag}"
    _unit(django_user_model, w["org"], unit_id)
    assert _bind(install_job_bind, job.pk, installer,
                 {"unit_id": unit_id, "claim_code": "claim-secret-1"}).status_code == 200

    saloon = Saloon.objects.create(org=w["org"], farm=w["farm"], name=f"map-saloon-{tag}")
    position = SaloonPosition.objects.create(
        saloon=saloon, org=w["org"], label="north"
    )
    sensor_slot = SaloonSensorSlot.objects.create(
        slot_id=f"SS-MAP-{tag}", saloon=saloon, org=w["org"], position=position,
        allowed_metric_keys=["temp", "humidity"],
    )
    return {"world": w, "installer": installer, "job": job, "unit_id": unit_id,
            "sensor_slot": sensor_slot}


def _map(job_id, user, payload):
    return _call_job(
        install_job_sensor_map, "POST", JOBS + f"{job_id}/sensors/", user, payload,
        job_id=job_id,
    )


def test_mapping_joins_the_pack_and_bumps_the_version(django_user_model):
    """The join that makes a sensor slot real: pack_id ↔ slot, and the device
    sees a NEW document because the version moved."""
    s = _bound_with_saloon(django_user_model, "0001")

    response = _map(s["job"].pk, s["installer"],
                    {"pack_id": "a1b2c3d4", "slot_id": s["sensor_slot"].slot_id})

    assert response.status_code == 201, response.data
    assert response.data["pack_id"] == "a1b2c3d4"
    assert response.data["config_version"] == 2  # 1 at bind + 1 for this sensor

    from apps.devices.models import GatewaySensorMapping

    row = GatewaySensorMapping.objects.get(pack_id="a1b2c3d4")
    assert row.slot_id == s["sensor_slot"].slot_id
    assert row.gateway_hardware_id == f"HW-GW-MAP-0001"

    from apps.devices.models import GatewaySlot

    slot = GatewaySlot.objects.get(pk="GW-MAP-0001")
    assert slot.desired_config_version == 2

    # and the sensor now reaches the desired document
    from apps.provisioning.desired_state import build_desired_state

    document = build_desired_state(slot)
    packs = [sensor["pack_id"] for sensor in document["sensors"]]
    assert "a1b2c3d4" in packs
    matched = next(s for s in document["sensors"] if s["pack_id"] == "a1b2c3d4")
    assert matched["saloon_id"] == s["sensor_slot"].saloon_id
    assert matched["metrics"] == ["temp", "humidity"]


def test_the_same_pack_twice_is_idempotent(django_user_model):
    s = _bound_with_saloon(django_user_model, "0002")
    payload = {"pack_id": "aa11bb22", "slot_id": s["sensor_slot"].slot_id}

    assert _map(s["job"].pk, s["installer"], payload).status_code == 201
    again = _map(s["job"].pk, s["installer"], payload)
    assert again.status_code == 200
    assert again.data["already_mapped"] is True

    from apps.devices.models import GatewaySlot

    assert GatewaySlot.objects.get(pk=s["unit_id"]).desired_config_version == 2


def test_one_physical_pack_can_only_be_in_one_place(django_user_model):
    """A sensor mounted twice is a data error the installer must see now."""
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Saloon

    s = _bound_with_saloon(django_user_model, "0003")
    w = s["world"]
    other_saloon = Saloon.objects.create(org=w["org"], farm=w["farm"], name="map-other")
    other_position = SaloonPosition.objects.create(
        saloon=other_saloon, org=w["org"], label="south"
    )
    other_slot = SaloonSensorSlot.objects.create(
        slot_id="SS-MAP-OTHER", saloon=other_saloon, org=w["org"], position=other_position
    )

    assert _map(s["job"].pk, s["installer"],
                {"pack_id": "c3d4e5f6", "slot_id": s["sensor_slot"].slot_id}).status_code == 201

    clash = _map(s["job"].pk, s["installer"],
                 {"pack_id": "c3d4e5f6", "slot_id": other_slot.slot_id})
    assert clash.status_code == 409
    assert clash.data["mapped_slot_id"] == s["sensor_slot"].slot_id

    from apps.devices.models import GatewaySensorMapping

    assert GatewaySensorMapping.objects.filter(pack_id="c3d4e5f6").count() == 1


def test_pack_id_must_be_the_8_hex_lora_node_id(django_user_model):
    s = _bound_with_saloon(django_user_model, "0004")
    for bad in ("P-A", "a1b2c3", "a1b2c3d4e5", "zzzzzzzz", ""):
        response = _map(s["job"].pk, s["installer"],
                        {"pack_id": bad, "slot_id": s["sensor_slot"].slot_id})
        assert response.status_code == 400, f"{bad!r} should be rejected"


def test_a_slot_in_another_farm_is_404(django_user_model):
    """The slot must be a mounting position in THIS job's farm, not any slot in
    the org — otherwise an installer could map a pack onto another building."""
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Farm, Saloon
    from apps.geography.models import City, Province

    s = _bound_with_saloon(django_user_model, "0005")
    w = s["world"]
    other_farm = Farm.objects.create(
        org=w["org"], name="map-farm-2", province=w["farm"].province, city=w["farm"].city
    )
    other_saloon = Saloon.objects.create(org=w["org"], farm=other_farm, name="map-elsewhere")
    position = SaloonPosition.objects.create(saloon=other_saloon, org=w["org"], label="x")
    remote = SaloonSensorSlot.objects.create(
        slot_id="SS-REMOTE", saloon=other_saloon, org=w["org"], position=position
    )

    response = _map(s["job"].pk, s["installer"],
                    {"pack_id": "d4e5f601", "slot_id": remote.slot_id})
    assert response.status_code == 404


def test_an_unbound_job_cannot_map_sensors(django_user_model):
    w = _world(django_user_model)
    installer = _installer(django_user_model, "map-unbound", w["org"])
    _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=w["jobs"][0].pk,
    )
    response = _map(w["jobs"][0].pk, installer, {"pack_id": "e5f60123", "slot_id": "SS-X"})
    assert response.status_code == 409
    assert "no gateway bound" in str(response.data)


def test_mapping_is_audited(django_user_model):
    s = _bound_with_saloon(django_user_model, "0006")
    _map(s["job"].pk, s["installer"],
         {"pack_id": "f6012345", "slot_id": s["sensor_slot"].slot_id})

    from apps.auditing.models import AccountActivityLog

    log = AccountActivityLog.objects.get(action="INSTALLER_JOB_SENSOR_MAP")
    assert log.org_id == s["world"]["org"].org_id
    assert log.details["pack_id"] == "f6012345"
    assert log.details["slot_id"] == s["sensor_slot"].slot_id
    assert PlatformAuditLog.objects.filter(action="PLATFORM_JOB_SENSOR_MAP").exists()


# ---------------------------------------------------------------------------
# task 2.9 — device photo uploads (AD-20)
# ---------------------------------------------------------------------------

def _photo(view, job_id, user, slot_id=None, **fields):
    path = (
        JOBS + f"{job_id}/sensors/{slot_id}/photo/"
        if slot_id
        else JOBS + f"{job_id}/gateway/photo/"
    )
    request = APIRequestFactory().post(path, data=fields, format="multipart")
    force_authenticate(request, user=user)
    request.jwt_claims = {}
    if slot_id:
        return view(request, job_id=job_id, slot_id=slot_id)
    return view(request, job_id=job_id)


def _sensor_slot_for(w, tag):
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Saloon

    saloon = Saloon.objects.create(org=w["org"], farm=w["farm"], name=f"ph-saloon-{tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=w["org"], label="north")
    return SaloonSensorSlot.objects.create(
        slot_id=f"SS-PH-{tag}", saloon=saloon, org=w["org"], position=position,
        allowed_metric_keys=["temp"],
    )


def _png(name="cam.png", size=2048):
    """A real PNG header — Django's content type is taken from the filename."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\n" + b"\x00" * size,
                              content_type="image/png")


def test_sensor_photo_upload_and_record(django_user_model):
    s = _bound_with_saloon(django_user_model, "ph1")
    w = s["world"]
    slot = _sensor_slot_for(w, "0001")

    response = _photo(install_sensor_photo, s["job"].pk, s["installer"],
                      slot_id=slot.slot_id, file=_png(), description="north wall")

    assert response.status_code == 201, response.data
    assert response.data["file_url"].startswith("/media/device-photos/")
    assert response.data["where"] == "sensor"
    assert response.data["target"] == slot.slot_id

    from django.core.files.storage import default_storage
    from apps.devices.models import DevicePhoto

    photo = DevicePhoto.objects.get(pk=response.data["photo_id"])
    assert photo.sensor_slot_id == slot.slot_id
    assert photo.gateway_slot_id is None  # AD-20: exactly one target
    assert photo.org_id == w["org"].org_id
    assert photo.uploaded_by_id == s["installer"].pk
    assert default_storage.exists(photo.file_url.removeprefix("/media/"))


def test_gateway_photo_upload_and_record(django_user_model):
    s = _bound_with_saloon(django_user_model, "ph2")

    response = _photo(install_gateway_photo, s["job"].pk, s["installer"], file=_png())

    assert response.status_code == 201, response.data
    from apps.devices.models import DevicePhoto

    photo = DevicePhoto.objects.get(pk=response.data["photo_id"])
    assert photo.gateway_slot_id == s["unit_id"]
    assert photo.sensor_slot_id is None


def test_a_photo_of_nothing_is_refused(django_user_model):
    """AD-20's CHECK constraint, enforced as a 400 rather than a 500."""
    s = _bound_with_saloon(django_user_model, "ph3")
    response = _store_device_photo(  # direct call: neither target
        _fake_request(s["installer"], _png()),
        org_id=str(s["world"]["org"].org_id), actor=s["installer"],
    )
    assert response.status_code == 400


def _fake_request(user, photo=None):
    request = APIRequestFactory().post(
        "/x", data={"file": photo} if photo else {}, format="multipart"
    )
    force_authenticate(request, user=user)
    request.jwt_claims = {}
    return request


@pytest.mark.parametrize("name,ctype", [
    ("notes.txt", "text/plain"),
    ("archive.zip", "application/zip"),
])
def test_bad_types_are_rejected(django_user_model, name, ctype):
    s = _bound_with_saloon(django_user_model, "ph4")
    slot = _sensor_slot_for(s["world"], "0004")
    from django.core.files.uploadedfile import SimpleUploadedFile

    response = _photo(install_sensor_photo, s["job"].pk, s["installer"],
                      slot_id=slot.slot_id,
                      file=SimpleUploadedFile(name, b"data", content_type=ctype))
    assert response.status_code == 400


def test_missing_file_is_400(django_user_model):
    s = _bound_with_saloon(django_user_model, "ph5")
    slot = _sensor_slot_for(s["world"], "0005")
    response = _photo(install_sensor_photo, s["job"].pk, s["installer"], slot_id=slot.slot_id)
    assert response.status_code == 400


def test_a_slot_in_another_farm_is_404(django_user_model):
    s = _bound_with_saloon(django_user_model, "ph6")
    w = s["world"]
    from apps.farms.models import Farm

    other_farm = Farm.objects.create(
        org=w["org"], name="ph-farm-2", province=w["farm"].province, city=w["farm"].city
    )
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Saloon

    saloon = Saloon.objects.create(org=w["org"], farm=other_farm, name="ph-elsewhere")
    position = SaloonPosition.objects.create(saloon=saloon, org=w["org"], label="x")
    remote = SaloonSensorSlot.objects.create(
        slot_id="SS-PH-REMOTE", saloon=saloon, org=w["org"], position=position
    )
    response = _photo(install_sensor_photo, s["job"].pk, s["installer"],
                      slot_id=remote.slot_id, file=_png())
    assert response.status_code == 404


def test_is_primary_clears_the_previous_primary(django_user_model):
    """One cover image per target — the invariant must stay total, not
    'the last uploader wins'."""
    s = _bound_with_saloon(django_user_model, "ph7")
    slot = _sensor_slot_for(s["world"], "0007")

    _photo(install_sensor_photo, s["job"].pk, s["installer"],
           slot_id=slot.slot_id, file=_png("one.png"), is_primary="true")
    _photo(install_sensor_photo, s["job"].pk, s["installer"],
           slot_id=slot.slot_id, file=_png("two.png"), is_primary="true")

    from apps.devices.models import DevicePhoto

    primaries = DevicePhoto.objects.filter(sensor_slot_id=slot.slot_id, is_primary=True)
    assert primaries.count() == 1
    assert DevicePhoto.objects.filter(sensor_slot_id=slot.slot_id).count() == 2


def test_gateway_photo_requires_a_bound_gateway(django_user_model):
    w = _world(django_user_model)
    installer = _installer(django_user_model, "ph-unbound", w["org"])
    _call_job(
        install_job_assign, "POST", JOBS + f"{w['jobs'][0].pk}/assign/", w["support"],
        {"installer_id": installer.pk}, job_id=w["jobs"][0].pk,
    )
    response = _photo(install_gateway_photo, w["jobs"][0].pk, installer, file=_png())
    assert response.status_code == 409
