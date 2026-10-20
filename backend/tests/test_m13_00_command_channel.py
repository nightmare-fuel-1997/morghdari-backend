"""Phase 3 — the command channel (closes D9).

The design doc's own audit found this subsystem had vanished by hand-off: v0.5
specified the whole thing (tasks E1-E6, Blueprint §11.2 state matrix), it was
deferred to a v0.6 that never picked it up, and nothing subscribed to the
response lane. `command_logs` had zero writers.

These tests pin the state machine and the two rules that are easy to get wrong:
a late response never resurrects a dead command, and an unknown command is
ignored rather than treated as a fault.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.provisioning.models import GatewayCommand, InstallationJob
from apps.provisioning.tasks import (
    reconcile_command_response,
    sweep_command_expiry,
)

pytestmark = pytest.mark.django_db

SLOT = "GW-CMD-0001"


@pytest.fixture
def world(django_user_model):
    from apps.devices.models import GatewaySlot
    from apps.farms.models import Farm
    from apps.geography.models import City, Province
    from apps.tenants.models import Organization

    province = Province.objects.create(name="P-CMD")
    city = City.objects.create(province=province, name="C-CMD")
    org = Organization.objects.create(name="cmd-org")
    farm = Farm.objects.create(org=org, name="cmd-farm", province=province, city=city)
    slot = GatewaySlot.objects.create(
        slot_id=SLOT, org=org, farm=farm, provisioning_status="PROVISIONED",
        desired_config_version=1, applied_config_version=1,
    )
    return {"org": org, "farm": farm, "slot": slot}


def _command(world, *, kind="REBOOT", state="SENT", expires_in=600, **extra):
    return GatewayCommand.objects.create(
        org=world["org"],
        gateway_slot=world["slot"],
        kind=kind,
        payload={"x": 1},
        state=state,
        expires_at=timezone.now() + timedelta(seconds=expires_in),
        **extra,
    )


def _respond(command, state, **extra):
    return reconcile_command_response(
        {"command_uuid": str(command.pk), "state": state, **extra},
        topic_slot_id=SLOT,
    )


# ---------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------

def test_ack_then_success(world):
    command = _command(world)

    assert _respond(command, "ACK") == "ACKNOWLEDGED"
    command.refresh_from_db()
    assert command.state == "ACKNOWLEDGED"
    assert command.acked_at is not None

    assert _respond(command, "SUCCESS") == "APPLIED"
    command.refresh_from_db()
    assert command.state == "APPLIED"
    assert command.applied_at is not None


def test_success_without_ack_is_still_applied(world):
    """Devices do not always send ACK — a SUCCESS alone is conclusive."""
    command = _command(world)
    assert _respond(command, "SUCCESS") == "APPLIED"
    command.refresh_from_db()
    assert command.state == "APPLIED"
    assert command.acked_at is not None  # back-filled


def test_failure_records_the_reason(world):
    command = _command(world)
    assert _respond(command, "FAILED", error="tamper switch open") == "FAILED"
    command.refresh_from_db()
    assert command.state == "FAILED"
    assert command.last_error == "tamper switch open"


def test_an_unknown_command_is_ignored_not_an_error(world):
    """Devices replay their outbox after a reconnect; that is normal."""
    outcome = reconcile_command_response(
        {"command_uuid": "00000000-0000-0000-0000-000000000000", "state": "SUCCESS"},
        topic_slot_id=SLOT,
    )
    assert outcome == "UNKNOWN_COMMAND"


def test_identity_mismatch_is_refused(world):
    """The topic segment is unforgeable; the payload's mirror must agree."""
    command = _command(world)
    outcome = reconcile_command_response(
        {"command_uuid": str(command.pk), "state": "SUCCESS"},
        topic_slot_id="GW-SOMETHING-ELSE",
    )
    assert outcome == "IDENTITY_MISMATCH"
    command.refresh_from_db()
    assert command.state == "SENT"  # untouched


@pytest.mark.parametrize("bad", [None, {}, {"state": "SUCCESS"}])
def test_malformed_bodies_are_rejected(world, bad):
    assert reconcile_command_response(bad, topic_slot_id=SLOT) == "MALFORMED"


# ---------------------------------------------------------------------------
# the late-response rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("terminal", ["FAILED", "EXPIRED"])
def test_a_late_success_never_resurrects_a_dead_command(world, terminal):
    """The rule that stops a dead command looking alive.

    A device that retries after the sweep expired its command must not flip the
    row to APPLIED — that would tell an operator a command succeeded when the
    system had already given up on it.
    """
    command = _command(world, state=terminal)
    outcome = _respond(command, "SUCCESS")
    assert outcome == "LATE_RESPONSE"

    command.refresh_from_db()
    assert command.state == terminal, "a terminal command was resurrected"
    assert "late" in (command.last_error or "")


# ---------------------------------------------------------------------------
# the expiry sweep (task 3.4)
# ---------------------------------------------------------------------------

def test_the_sweep_expires_overdue_commands(world):
    stale = _command(world, state="SENT", expires_in=-60)
    fresh = _command(world, state="SENT", expires_in=600)

    assert sweep_command_expiry() >= 1

    stale.refresh_from_db()
    fresh.refresh_from_db()
    assert stale.state == "EXPIRED"
    assert "expired" in (stale.last_error or "").lower()
    assert fresh.state == "SENT", "a command still in date was expired"


def test_the_sweep_leaves_terminal_states_alone(world):
    done = _command(world, state="APPLIED", expires_in=-60)
    failed = _command(world, state="FAILED", expires_in=-60)

    sweep_command_expiry()

    done.refresh_from_db()
    failed.refresh_from_db()
    assert done.state == "APPLIED"
    assert failed.state == "FAILED"


# ---------------------------------------------------------------------------
# task 3.2 — ROTATE_CREDENTIAL commits only on proof
# ---------------------------------------------------------------------------

def test_rotate_commits_only_when_the_device_presents_the_new_password(world):
    from apps.mqtt_auth.models import MqttDeviceCredential
    from apps.mqtt_auth.services import hash_password, stage_rotation

    MqttDeviceCredential.objects.create(
        device_id=SLOT, username=SLOT,
        password_hash=hash_password("old-pass"), is_active=True,
    )
    stage_rotation(SLOT, "new-pass")
    command = _command(world, kind="ROTATE_CREDENTIAL")

    # A bare SUCCESS is NOT proof the device can authenticate with the new
    # password, so the rotation must stay staged.
    assert _respond(command, "SUCCESS") == "APPLIED"
    credential = MqttDeviceCredential.objects.get(username=SLOT)
    assert credential.pending_password_hash is not None, "committed without proof"
    assert credential.last_rotated_at is None

    # A second command that DOES carry the password commits it.
    second = _command(world, kind="ROTATE_CREDENTIAL")
    assert _respond(second, "SUCCESS", password="new-pass") == "APPLIED"
    credential.refresh_from_db()
    assert credential.pending_password_hash is None
    assert credential.last_rotated_at is not None


def test_rotate_with_a_wrong_password_does_not_commit(world):
    from apps.mqtt_auth.models import MqttDeviceCredential
    from apps.mqtt_auth.services import hash_password, stage_rotation

    MqttDeviceCredential.objects.create(
        device_id=SLOT, username=SLOT,
        password_hash=hash_password("old-pass"), is_active=True,
    )
    stage_rotation(SLOT, "new-pass")
    command = _command(world, kind="ROTATE_CREDENTIAL")

    assert _respond(command, "SUCCESS", password="guess") == "APPLIED"
    credential = MqttDeviceCredential.objects.get(username=SLOT)
    assert credential.pending_password_hash is not None


# ---------------------------------------------------------------------------
# task 3.3 — diagnostics are kept for an operator
# ---------------------------------------------------------------------------

def test_diagnostics_report_is_recorded(world):
    from apps.auditing.models import AccountActivityLog

    command = _command(world, kind="DIAGNOSTICS_REQUEST")
    assert _respond(command, "SUCCESS", result={"uptime_s": 42, "rssi": -71}) == "APPLIED"

    log = AccountActivityLog.objects.get(action="GATEWAY_DIAGNOSTICS_REPORT")
    assert log.org_id == world["org"].org_id
    assert log.details["report"]["uptime_s"] == 42
    assert log.details["gateway_slot_id"] == SLOT


def test_an_unknown_kind_cannot_even_be_stored(world):
    """The defence against an unknown kind is structural, not defensive code.

    `ck_gateway_commands_kind` rejects anything outside the KINDS list at the
    storage layer, so the reconciler can never meet one. That is stronger than
    a no-op branch in `_on_command_applied` (which still exists, as the second
    line of defence, for a kind added to the model but not to the reconciler).
    """
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        _command(world, kind="SOME_FUTURE_KIND")


def test_a_known_kind_with_no_reconciler_branch_is_a_harmless_no_op(world):
    """PROVISION/REBOOT/REFRESH have no per-kind effect beyond the transition."""
    command = _command(world, kind="REBOOT")
    assert _respond(command, "SUCCESS") == "APPLIED"
    command.refresh_from_db()
    assert command.state == "APPLIED"


# ---------------------------------------------------------------------------
# task 3.1 — the subscriber routes the lane
# ---------------------------------------------------------------------------

def test_the_subscriber_recognises_the_response_lane():
    from morghdari.subscriber.main import _is_command_response_topic

    assert _is_command_response_topic(
        "83b266c0-eb84-473d-afd6-4b6f2feafabe/farms/1/gateways/GW-1/commands/response"
    )
    # the telemetry lane must not be mistaken for a response
    assert not _is_command_response_topic(
        "83b266c0-eb84-473d-afd6-4b6f2feafabe/farms/1/gateways/GW-1/telemetry"
    )
    # nor the command DOWNLINK lane
    assert not _is_command_response_topic(
        "83b266c0-eb84-473d-afd6-4b6f2feafabe/farms/1/gateways/GW-1/commands"
    )
