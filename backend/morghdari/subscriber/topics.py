"""Topic identity — the transport is the truth (TUTORIAL §3.1).

org/farm/gateway come FROM THE TOPIC because EMQX authenticated the publishing
connection; the payload's single mirrored field `gateway_slot_id` must MATCH
the topic segment or the message is an identity fault → dead-letter.
فارسی: هویت از تاپیک می‌آید؛ تنها فیلدِ محموله که آینهٔ تاپیک است باید با آن
بخواند وگرنه پیامِ جعلی است و به صندوق مرده می‌رود.
"""

import re


class TopicParseError(ValueError):
    """Raised for any topic that does not match the canonical T1 shape."""


_TOPIC_RE = re.compile(
    r"^(?P<org_id>[^/]+)/farms/(?P<farm_id>[^/]+)/gateways/"
    r"(?P<gateway_slot>[^/]+)/telemetry$"
)


def parse_topic(topic: str) -> tuple[str, str, str]:
    """Return (org_id, farm_id, gateway_slot); raise TopicParseError otherwise."""
    match = _TOPIC_RE.match(topic or "")
    if match is None:
        raise TopicParseError(f"topic does not match T1 shape: {topic!r}")
    return (
        match.group("org_id"),
        match.group("farm_id"),
        match.group("gateway_slot"),
    )


IDENTITY_MISMATCH = "TOPIC_PAYLOAD_IDENTITY_MISMATCH"


def identity_fault(gateway_slot_from_topic: str, payload: dict) -> str | None:
    """Return the DLQ reason when the mirrored slot id disagrees (or is absent)."""
    if payload.get("gateway_slot_id") != gateway_slot_from_topic:
        return IDENTITY_MISMATCH
    return None
