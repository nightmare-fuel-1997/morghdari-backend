"""Regression tests for the two defects fixed during Phase 1.

Both were confirmed by reading code, not by inference:

  D1 — the gateway status lane was subscribed since v0.3 but never routed: it
  fell into the telemetry pipeline, which rejects anything not ending in
  `/telemetry`, so every ONLINE/OFFLINE publish and every LWT was dead-lettered
  as `bad_topic`. `_handle_status` (`subscriber/main.py`) existed and was never
  called.

  D3 — `gateway_acl_rule_items()` interpolated the nullable `farm_id` straight
  into the topic string, so a slot with no farm yet produced
  `{org}/farms/None/gateways/{slot}/...` — a rule that matches no topic and
  silently denies everything. This is exactly the state the new factory flow
  creates, so it had to be fixed before Phase 2.
"""

import json

import pytest

from apps.provisioning.api import gateway_acl_rule_items

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# D3: a farm-less slot must get no tenant rules at all
# ---------------------------------------------------------------------------

def test_farmless_slot_gets_no_acls_instead_of_a_none_topic():
    items = gateway_acl_rule_items("9f8c2d34-0000-0000-0000-000000000000", None, "GW-0042")
    assert items == []
    for item in gateway_acl_rule_items("org", None, "GW-0042"):
        assert "None" not in item["topic"]


def test_slot_with_a_farm_still_gets_the_full_set():
    items = gateway_acl_rule_items("org-uuid", 12, "GW-0042")
    assert len(items) == 9
    assert all(item["permission"] == "allow" for item in items)
    assert all("farms/None" not in item["topic"] for item in items)


# ---------------------------------------------------------------------------
# D1/D2: the status lane
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Minimal Redis stand-in: only what the two lanes touch."""

    def __init__(self):
        self.sets = {}
        self.added = []
        self._cache = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self._cache:
            return False
        self._cache[key] = value
        self.sets[key] = value
        return True

    def get(self, key):
        return self._cache.get(key)

    def xadd(self, stream, fields):
        self.added.append((stream, fields))

    def xtrim(self, *args, **kwargs):
        return 0


class _Message:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


def _status_payload(slot_id, status):
    return json.dumps(
        {"status": status, "gateway_slot_id": slot_id,
         "gateway_hardware_id": "AA:BB", "ts": "2026-10-18T08:00:00Z"}
    ).encode()


def _patched_main(monkeypatch, fake=None):
    from morghdari.subscriber import main

    fake = fake or _FakeRedis()
    monkeypatch.setattr(main, "_redis", lambda: fake)
    return main, fake


@pytest.mark.parametrize(
    "topic",
    [
        "org/farms/12/gateways/GW-1/status",
        "9f8c2d34-1111-2222-3333-444455556666/farms/3/gateways/GW-2026-0042/status",
    ],
)
def test_status_topic_is_recognised(topic):
    from morghdari.subscriber.main import _is_status_topic

    assert _is_status_topic(topic) is True


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "devices/GW-1/reported",
        "org/farms/12/gateways/GW-1/telemetry",
        "org/farms/12/gateways/GW-1/status/extra",
        "org/farms/12/gateways/GW-1/commands",
    ],
)
def test_non_status_topics_are_not_captured(topic):
    from morghdari.subscriber.main import _is_status_topic

    assert _is_status_topic(topic) is False


def test_status_message_is_routed_to_the_status_lane_not_the_dlq(monkeypatch):
    """The regression: it used to reach handle_message and be dead-lettered."""
    main, fake = _patched_main(monkeypatch)

    main._on_message(
        None, None, _Message("org/farms/1/gateways/GW-1/status", _status_payload("GW-1", "ONLINE"))
    )

    from apps.alerts.services.redis_keys import gateway_lwt_key

    assert fake.sets.get(gateway_lwt_key("org", "GW-1")) == "online"
    assert fake.added == []  # nothing dead-lettered, nothing ingested


def test_status_lane_accepts_uppercase_and_lowercase(monkeypatch):
    from apps.alerts.services.redis_keys import gateway_lwt_key

    for published, stored in (("ONLINE", "online"), ("OFFLINE", "offline"),
                              ("online", "online")):
        main, fake = _patched_main(monkeypatch)
        main._on_message(
            None, None,
            _Message("org/farms/1/gateways/GW-1/status", _status_payload("GW-1", published)),
        )
        assert fake.sets.get(gateway_lwt_key("org", "GW-1")) == stored


def test_status_identity_mismatch_is_dropped(monkeypatch):
    """The payload mirrors the topic; disagreement is an identity fault."""
    main, fake = _patched_main(monkeypatch)
    main._on_message(
        None, None,
        _Message("org/farms/1/gateways/GW-1/status", _status_payload("GW-OTHER", "ONLINE")),
    )
    assert fake.sets == {}


def test_unknown_status_value_is_ignored(monkeypatch):
    main, fake = _patched_main(monkeypatch)
    main._on_message(
        None, None, _Message("org/farms/1/gateways/GW-1/status", b'{"status": "MAYBE"}')
    )
    assert fake.sets == {}


def test_malformed_status_json_does_not_crash(monkeypatch):
    main, fake = _patched_main(monkeypatch)
    main._on_message(None, None, _Message("org/farms/1/gateways/GW-1/status", b"{not json"))
    assert fake.sets == {}


def test_telemetry_still_reaches_the_pipeline(monkeypatch):
    """The routing branch must not swallow real telemetry."""
    main, fake = _patched_main(monkeypatch)
    payload = json.dumps({
        "gateway_slot_id": "GW-1", "gateway_hardware_id": "AA:BB",
        "schema_version": "2.0", "device_ts": "2026-10-18T08:00:00Z",
        "readings": [{"pack_id": "P-A", "metrics": {}}],
    }).encode()

    # db_fetch/status_fetch are patched so the pipeline runs without a broker.
    monkeypatch.setattr(main, "_slot_db_fetch", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_status_fetch", lambda _gw: None)
    main._on_message(None, None, _Message("org/farms/1/gateways/GW-1/telemetry", payload))

    assert fake.added, "telemetry must still be written to a stream"
