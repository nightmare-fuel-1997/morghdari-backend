"""Device-lane topics — the pre-org control channel (AD-3, AD-19, 2026-10-18).

WHY a device-rooted namespace instead of the tenant-prefixed one: an unbound
device has no `org_id`, so it cannot compute `{org}/farms/{farm}/gateways/...`
— the chicken-and-egg the redesign exists to break. `devices/{slot_id}/...` is
rooted at the identity the device always has (AD-2: the factory-assigned
`unit_id`).

This is a deliberate, documented exception to "org_id prefixes every topic"
(MQTT Topic Map §6). That rule exists so the first topic segment is unforgeable
org evidence for INGESTED TELEMETRY (`morghdari/subscriber/topics.py`); these
three topics carry no telemetry, and the ACL compensates by pinning every
gateway username to its own literal topics — no wildcards anywhere.

Category split (AD-19):
  * D1/D2/D3 = Category A, device management — must work BEFORE the device has
    an org (provisioning, rotation, diagnostics).
  * T3/T4     = Category B, actuator commands — saloon-scoped, post-provisioning.
"""

DESIRED_SUFFIX = "desired"
REPORTED_SUFFIX = "reported"
COMMAND_SUFFIX = "command"

DEVICE_ROOT = "devices"


class DeviceTopicError(ValueError):
    """Raised for a topic that does not match the device-lane shape."""


def desired_topic(slot_id: str) -> str:
    """D1 — retained desired configuration (backend → gateway)."""
    return f"{DEVICE_ROOT}/{slot_id}/{DESIRED_SUFFIX}"


def reported_topic(slot_id: str) -> str:
    """D2 — config apply reports, command ACKs, heartbeat (gateway → backend)."""
    return f"{DEVICE_ROOT}/{slot_id}/{REPORTED_SUFFIX}"


def command_topic(slot_id: str) -> str:
    """D3 — Category A one-shot commands (backend → gateway, not retained)."""
    return f"{DEVICE_ROOT}/{slot_id}/{COMMAND_SUFFIX}"


def parse_device_topic(topic: str) -> tuple[str, str]:
    """Return (slot_id, suffix) for a device-lane topic; raise otherwise.

    The subscriber uses this to route D2 messages into the reconciler, and the
    reconciler re-checks that the payload's `slot_id` equals the topic segment
    (same identity-fault rule the telemetry lane already enforces).
    """
    parts = (topic or "").split("/")
    if len(parts) != 3 or parts[0] != DEVICE_ROOT:
        raise DeviceTopicError(f"not a device-lane topic: {topic!r}")
    if parts[2] not in (DESIRED_SUFFIX, REPORTED_SUFFIX, COMMAND_SUFFIX):
        raise DeviceTopicError(f"unknown device-lane suffix: {topic!r}")
    return parts[1], parts[2]


def reported_subscription() -> str:
    """Shared subscription filter for the subscriber tier (mirrors S1)."""
    return f"$share/telemetry_workers/{DEVICE_ROOT}/+/{REPORTED_SUFFIX}"
