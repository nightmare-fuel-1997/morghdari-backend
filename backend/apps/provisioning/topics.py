"""Device-lane topics — the pre-org control channel (AD-3, AD-19, 2026-10-18).

WHY a device-rooted namespace instead of the tenant-prefixed one: an unbound
device has no `org_id`, so it cannot compute `{org}/farms/{farm}/gateways/...`
— the chicken-and-egg the redesign exists to break. `devices/{slot_id}/...` is
rooted at the identity the device always has (AD-2: the factory-assigned
`unit_id`).

This is a deliberate, documented exception to "org_id prefixes every topic"
(MQTT Topic Map §6). That rule exists so the first topic segment is unforgeable
org evidence for INGESTED TELEMETRY (`morghdari/subscriber/topics.py`); these
four topics carry no telemetry, and the ACL compensates by pinning every
gateway username to its own literal topics — no wildcards anywhere.

Category split (AD-19):
  * D1/D2/D3 = Category A, device management — must work BEFORE the device has
    an org (provisioning, rotation, diagnostics).
  * T3/T4     = Category B, actuator commands — saloon-scoped, post-provisioning.
"""

DESIRED_SUFFIX = "desired"
REPORTED_SUFFIX = "reported"
COMMAND_SUFFIX = "command"
COMMAND_RESPONSE_SUFFIX = "command/response"

DEVICE_ROOT = "devices"


class DeviceTopicError(ValueError):
    """Raised for a topic that does not match the device-lane shape."""


def desired_topic(slot_id: str) -> str:
    """D1 — retained desired configuration (backend → gateway)."""
    return f"{DEVICE_ROOT}/{slot_id}/{DESIRED_SUFFIX}"


def reported_topic(slot_id: str) -> str:
    """D2 — config apply reports, heartbeat (gateway → backend).

    Command ACKs moved OFF here in 2026-10-20 to their own lane — see
    `command_response_topic`. The original D2 docstring folded them together,
    which made routing depend on sniffing the payload; the tenant side has
    always had a dedicated `…/commands/response`, so the device lane now matches.
    """
    return f"{DEVICE_ROOT}/{slot_id}/{REPORTED_SUFFIX}"


def command_topic(slot_id: str) -> str:
    """D3 — Category A one-shot commands (backend → gateway, not retained)."""
    return f"{DEVICE_ROOT}/{slot_id}/{COMMAND_SUFFIX}"


def command_response_topic(slot_id: str) -> str:
    """D4 — the device's answer to a D3 command (gateway → backend).

    NOT retained and NOT `reported`: a command response belongs to exactly one
    command_uuid, so it must not be conflated with the retained-style config
    report the reconciler treats as the gateway's current state. Separating the
    two also means a device that never receives a command never appears to have
    answered one.

    Before the device is bound there is no org, so this topic exists at all:
    the tenant lane (`{org}/farms/{farm}/gateways/{slot}/commands/response`) is
    only reachable after binding.
    """
    return f"{DEVICE_ROOT}/{slot_id}/{COMMAND_RESPONSE_SUFFIX}"


def parse_device_topic(topic: str) -> tuple[str, str]:
    """Return (slot_id, suffix) for a device-lane topic; raise otherwise.

    Accepts the four-level `command/response` lane by normalising it to the
    `command/response` suffix, so callers that dispatch on the suffix see one
    string rather than having to reassemble two segments.
    """
    parts = (topic or "").split("/")
    if len(parts) not in (3, 4) or parts[0] != DEVICE_ROOT:
        raise DeviceTopicError(f"not a device-lane topic: {topic!r}")
    if len(parts) == 3:
        if parts[2] not in (DESIRED_SUFFIX, REPORTED_SUFFIX, COMMAND_SUFFIX):
            raise DeviceTopicError(f"unknown device-lane suffix: {topic!r}")
        return parts[1], parts[2]
    if parts[2] == COMMAND_SUFFIX and parts[3] == "response":
        return parts[1], COMMAND_RESPONSE_SUFFIX
    raise DeviceTopicError(f"unknown device-lane suffix: {topic!r}")


def reported_subscription() -> str:
    """Shared subscription filter for the subscriber tier (mirrors S1)."""
    return f"$share/telemetry_workers/{DEVICE_ROOT}/+/{REPORTED_SUFFIX}"


def command_response_subscription() -> str:
    """Shared subscription filter for the D4 command-response lane.

    Shared for the same reason telemetry is: each response belongs to one
    command, so with more than one replica it must reach exactly one of them.
    """
    return f"$share/telemetry_workers/{DEVICE_ROOT}/+/{COMMAND_RESPONSE_SUFFIX}"
