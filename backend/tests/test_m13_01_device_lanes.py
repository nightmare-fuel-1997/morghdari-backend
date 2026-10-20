"""Regression tests for the missing DEVICE lanes — found 2026-10-20.

The defect, confirmed by reading `subscriber/main.py` rather than by inference:

  The subscriber's only subscriptions were the three TENANT lanes
  (`+/farms/+/gateways/+/telemetry` shared, `…/status`, `…/commands/response`).
  Nothing subscribed to `devices/{slot}/reported` — the org-less uplink AD-3
  defines, and the only way a factory unit can report that it applied its
  configuration before it has been bound to a farm.

  Consequence: `reconcile_reported()` was defined, referenced by tests, and
  called by nothing in production. The gateway slot's `applied_config_version`
  never moved, so the submit → PROVISIONED check (applied == desired) could
  never pass. Every test that appeared to prove the configure path was calling
  `reconcile_reported()` directly, bypassing the broker entirely — which is why
  a service that could not work in production was green everywhere.

  This is the D9 shape (Phase 3's command-response lane was never subscribed
  either) but worse: D9 left the channel missing entirely, while this one left
  it LOOKING finished. The lesson is the same one the status lane taught (D1):
  a subscription list is a contract, and it must be asserted.

The device command-response lane (`devices/{slot}/command/response`) was missing
for the same reason: an unbound device has no org, so it cannot answer a command
on the tenant lane, and its own lane was unlistened.
"""

import json

import pytest

from morghdari.subscriber import main as sub


class _Message:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


def _reported_payload(slot_id, config_version=7, state="APPLIED"):
    return json.dumps(
        {"schema_version": "1.0", "slot_id": slot_id,
         "config_version": config_version, "state": state,
         "error": None, "ts": "2026-10-20T09:15:00Z"}
    ).encode()


def _command_response_payload(slot_id, command_uuid, state="SUCCESS"):
    return json.dumps(
        {"schema_version": "1.0", "slot_id": slot_id,
         "command_uuid": command_uuid, "state": state,
         "result": {"ok": True}}
    ).encode()


# ---------------------------------------------------------------------------
# 1. The subscriptions must EXIST — the defect itself
# ---------------------------------------------------------------------------

def test_the_two_device_lanes_are_subscribed(monkeypatch):
    """A lane that is not subscribed is a lane that cannot work, no matter how
    correct its handler is. This is the assertion whose absence let the defect
    ship."""
    calls = []

    class _Client:
        def subscribe(self, topic, qos=0):
            calls.append((topic, qos))
            return (0, len(calls) + 1)

    monkeypatch.setattr(sub.mqtt, "Client", lambda *a, **k: _Client())
    monkeypatch.setenv("SUBSCRIBER_CLIENT_ID", "test-subscriber")

    sub._request_subscriptions(_Client())

    topics = [t for t, _ in calls]
    assert sub.REPORTED_DEVICE_TOPIC in topics
    assert sub.COMMAND_RESPONSE_DEVICE_TOPIC in topics
    # shared group: with more than one replica each message must reach exactly one
    assert sub.REPORTED_DEVICE_TOPIC.startswith("$share/")
    assert sub.COMMAND_RESPONSE_DEVICE_TOPIC.startswith("$share/")


def test_readiness_requires_all_five_lanes(clean_sub_state):
    """`_subscriptions_ok()` gates the health gauge. Partial readiness is
    DOWN readiness: a subscriber holding four of five lanes is not ingesting
    everything, and the gauge must say so."""
    lanes = (
        sub.SHARED_TOPIC,
        sub.STATUS_TOPIC,
        sub.COMMAND_RESPONSE_TOPIC,
        sub.REPORTED_DEVICE_TOPIC,
        sub.COMMAND_RESPONSE_DEVICE_TOPIC,
    )
    for added, topic in enumerate(lanes, start=1):
        sub._granted_topics.add(topic)
        # only the LAST lane completes readiness; any subset is DOWN
        assert sub._subscriptions_ok() is (added == len(lanes))

    # and losing any single lane takes it back down — checked for each, not
    # just one, because a hand-written set literal is easy to get half right.
    for missing in lanes:
        remaining = set(lanes) - {missing}
        sub._granted_topics.clear()
        sub._granted_topics.update(remaining)
        assert sub._subscriptions_ok() is False, f"missing {missing} should be DOWN"

    sub._granted_topics.update(lanes)
    assert sub._subscriptions_ok() is True


# ---------------------------------------------------------------------------
# 2. Topic recognition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "topic",
    [
        "devices/GW-0401/reported",
        "devices/9f8c2d34-1111-2222/reported",
    ],
)
def test_reported_device_topic_is_recognised(topic):
    assert sub._is_reported_device_topic(topic) is True


@pytest.mark.parametrize(
    "topic",
    [
        "devices/GW-1/report",
        "devices/GW-1/reported/extra",
        "org/farms/1/gateways/GW-1/reported",
        "devices/GW-1/command/response",
    ],
)
def test_non_reported_devices_are_not_captured(topic):
    assert sub._is_reported_device_topic(topic) is False


@pytest.mark.parametrize(
    "topic",
    [
        "devices/GW-0401/command/response",
        "devices/9f8c2d34-1111-2222/command/response",
    ],
)
def test_command_response_device_topic_is_recognised(topic):
    assert sub._is_command_response_device_topic(topic) is True


# ---------------------------------------------------------------------------
# 3. Routing — the handler must actually be reached
# ---------------------------------------------------------------------------

def test_reported_message_reaches_the_reconciler_not_the_dlq(monkeypatch):
    seen = {}

    def _fake_reconcile(payload, topic_slot_id=None):
        seen["payload"] = payload
        seen["slot"] = topic_slot_id

    monkeypatch.setattr(
        "apps.provisioning.tasks.reconcile_reported", _fake_reconcile
    )
    ingested = []
    monkeypatch.setattr(sub, "handle_message", lambda *a, **k: ingested.append(a))

    sub._on_message(
        None, None,
        _Message("devices/GW-0401/reported", _reported_payload("GW-0401")),
    )

    assert seen.get("slot") == "GW-0401"
    assert seen["payload"]["config_version"] == 7
    assert seen["payload"]["state"] == "APPLIED"
    assert ingested == [], "the device lane must never reach the telemetry pipeline"


def test_device_command_response_reaches_the_reconciler(monkeypatch):
    seen = {}

    def _fake_reconcile(payload, topic_slot_id=None):
        seen["payload"] = payload
        seen["slot"] = topic_slot_id

    monkeypatch.setattr(
        "apps.provisioning.tasks.reconcile_command_response", _fake_reconcile
    )
    ingested = []
    monkeypatch.setattr(sub, "handle_message", lambda *a, **k: ingested.append(a))

    sub._on_message(
        None, None,
        _Message(
            "devices/GW-0401/command/response",
            _command_response_payload("GW-0401", "cmd-uuid-1"),
        ),
    )

    assert seen.get("slot") == "GW-0401"
    assert seen["payload"]["command_uuid"] == "cmd-uuid-1"
    assert seen["payload"]["state"] == "SUCCESS"
    assert ingested == []


def test_the_tenant_command_lane_still_works(monkeypatch):
    """Phase 3's lane must not regress when the device one is added."""
    seen = {}

    def _fake_reconcile(payload, topic_slot_id=None):
        seen["payload"] = payload
        seen["slot"] = topic_slot_id

    monkeypatch.setattr(
        "apps.provisioning.tasks.reconcile_command_response", _fake_reconcile
    )
    monkeypatch.setattr(sub, "handle_message", lambda *a, **k: None)

    sub._on_message(
        None, None,
        _Message(
            "org/farms/1/gateways/GW-9/commands/response",
            _command_response_payload("GW-9", "cmd-uuid-2"),
        ),
    )

    assert seen.get("slot") == "GW-9"
    assert seen["payload"]["command_uuid"] == "cmd-uuid-2"


# ---------------------------------------------------------------------------
# 4. Robustness — a malformed message must never take the lane down
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [b"{not json", b"", b"null", b"[]"])
def test_malformed_device_messages_do_not_crash(monkeypatch, payload):
    monkeypatch.setattr(
        "apps.provisioning.tasks.reconcile_reported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "apps.provisioning.tasks.reconcile_command_response", lambda *a, **k: None
    )
    # a raw non-dict must be handed on, not raised: the reconciler decides
    sub._on_message(None, None, _Message("devices/GW-0401/reported", payload))
