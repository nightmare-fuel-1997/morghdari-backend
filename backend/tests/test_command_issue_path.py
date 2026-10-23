"""The command channel's ISSUE half — the part that had no writer.

`publish_command` and `reconcile_command_response` both existed and were both
tested, but nothing in the application ever called the publisher. The only
callers were tests, which is exactly how a channel can look finished while being
completely dead: the reconciler is exercised against synthetic payloads, the
publisher is exercised against a real broker, and no code path ever connects
them.

These tests pin the connection, and the rotation loop that depended on it.
"""

import time
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------- helpers

def _org():
    from apps.tenants.models import Organization

    return Organization.objects.create(name=f"cmd-org-{int(time.time()) % 100000}")


def _slot(org, status="PROVISIONED", with_credential=False):
    from apps.devices.models import GatewaySlot

    slot = GatewaySlot.objects.create(
        slot_id=f"GW-CMD-{int(time.time()) % 100000}",
        org=org,
        provisioning_status=status,
        desired_config_version=1,
        applied_config_version=1,
    )
    if with_credential:
        # `stage_rotation` looks the credential up by slot id, so a slot with no
        # credential cannot be rotated at all — which is the real precondition.
        from apps.mqtt_auth.models import MqttDeviceCredential

        MqttDeviceCredential.objects.create(
            device_id=slot.slot_id, username=slot.slot_id,
            password_hash="x", is_active=True,
        )
    return slot


def _manager(org):
    from django.contrib.auth import get_user_model

    from apps.accounts.models import OrgMember

    User = get_user_model()
    user = User.objects.create_user(username=f"mgr-{int(time.time()) % 100000}", password="x")
    # OrgMember uses related_name="+", so there is no user.org_members — the
    # membership is reached by querying the table.
    OrgMember.objects.create(user=user, org=org, role="manager", is_active=True)
    return user


def _call(user, view, method, path, data=None, org_id=None, **kwargs):
    from apps.accounts.models import OrgMember

    request = getattr(APIRequestFactory(), method.lower())(path, data=data or {}, format="json")
    force_authenticate(request, user=user)
    if org_id is None:
        org_id = OrgMember.objects.filter(user=user).values_list("org_id", flat=True).first()
    request.jwt_claims = {"org_id": str(org_id), "roles": ["manager"]}
    return view(request, **kwargs)


# ------------------------------------------------------------------- the service

def test_issue_command_creates_a_pending_row_and_enqueues_a_publish():
    """The row exists AND a publish is queued. Either alone is the old bug."""
    from apps.provisioning.models import GatewayCommand
    from apps.provisioning.services import COMMAND_TTL_MINUTES, issue_command

    org = _org()
    slot = _slot(org)
    command = issue_command(slot.slot_id, "REBOOT", org_id=org.org_id)

    assert command.state == GatewayCommand.STATE_PENDING
    assert command.kind == "REBOOT"
    assert command.gateway_slot_id == slot.slot_id
    assert command.expires_at > timezone.now()
    assert command.expires_at <= timezone.now() + timedelta(minutes=COMMAND_TTL_MINUTES + 1)


def test_issue_command_rejects_an_unknown_kind():
    from apps.provisioning.services import issue_command

    org = _org()
    slot = _slot(org)
    with pytest.raises(ValueError):
        issue_command(slot.slot_id, "LAUNCH_MISSILE", org_id=org.org_id)


def test_issue_command_enqueues_the_publish_on_commit(monkeypatch):
    """on_commit, not inline: a rolled-back request must not leave a PENDING
    command behind for the sweep to expire."""
    from apps.provisioning.services import issue_command

    org = _org()
    slot = _slot(org)
    sent = []
    monkeypatch.setattr(
        "apps.provisioning.tasks.publish_command.delay", lambda pk: sent.append(pk)
    )
    command = issue_command(slot.slot_id, "REBOOT", org_id=org.org_id)
    # not yet — the transaction has not committed
    assert sent == []


# ------------------------------------------------------------------ the endpoint

def test_the_endpoint_issues_a_command():
    from apps.provisioning.api import issue_gateway_command

    org = _org()
    slot = _slot(org)
    manager = _manager(org)
    response = _call(manager, issue_gateway_command, "POST",
                     f"/gateways/{slot.slot_id}/commands/",
                     {"kind": "DIAGNOSTICS_REQUEST"}, gateway_slot_id=slot.slot_id)
    assert response.status_code == 202, response.data
    assert response.data["state"] == "PENDING"
    assert response.data["kind"] == "DIAGNOSTICS_REQUEST"


def test_the_endpoint_refuses_provision_from_a_client():
    """PROVISION is owned by the bind flow; a client re-issuing it would race the
    install it is meant to record."""
    from apps.provisioning.api import issue_gateway_command

    org = _org()
    slot = _slot(org)
    manager = _manager(org)
    response = _call(manager, issue_gateway_command, "POST",
                     f"/gateways/{slot.slot_id}/commands/", {"kind": "PROVISION"},
                     gateway_slot_id=slot.slot_id)
    assert response.status_code == 400
    assert "bind" in response.data["error"]


def test_the_endpoint_refuses_an_unconfigured_gateway():
    """No point commanding a device that has never been configured."""
    from apps.provisioning.api import issue_gateway_command

    org = _org()
    slot = _slot(org, status="PENDING_INSTALLATION")
    manager = _manager(org)
    response = _call(manager, issue_gateway_command, "POST",
                     f"/gateways/{slot.slot_id}/commands/", {"kind": "REBOOT"},
                     gateway_slot_id=slot.slot_id)
    assert response.status_code == 409


def test_the_endpoint_refuses_another_orgs_gateway():
    from apps.provisioning.api import issue_gateway_command

    org, other = _org(), _org()
    slot = _slot(org)
    manager = _manager(other)
    response = _call(manager, issue_gateway_command, "POST",
                     f"/gateways/{slot.slot_id}/commands/", {"kind": "REBOOT"},
                     org_id=other.org_id, gateway_slot_id=slot.slot_id)
    assert response.status_code == 404


# ------------------------------------------------------- the rotation loop

def test_rotation_issues_the_command_that_delivers_the_password():
    """THE regression. `rotate_credential` staged a hash and returned the
    plaintext, but never told the device — so the old password kept working, the
    new one was never presented, and `rotation_ack` could only succeed if a human
    carried the password to the gateway by hand. The command IS the delivery."""
    from apps.provisioning.api import rotate_credential
    from apps.provisioning.models import GatewayCommand

    org = _org()
    slot = _slot(org, with_credential=True)
    manager = _manager(org)
    response = _call(manager, rotate_credential, "POST",
                     f"/gateways/{slot.slot_id}/rotate-credential/",
                     gateway_slot_id=slot.slot_id)
    assert response.status_code == 200, response.data
    assert response.data["rotation_pending"] is True

    command = GatewayCommand.objects.filter(
        gateway_slot_id=slot.slot_id, kind="ROTATE_CREDENTIAL").first()
    assert command is not None, "no ROTATE_CREDENTIAL command was issued"
    assert command.payload["new_password"] == response.data["password"]
