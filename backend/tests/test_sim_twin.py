"""Unit tests for the digital-twin gateway (`simulator/twin.py`).

These run without a broker: the twin's MQTT client is replaced by a recorder,
so what is under test is the DEVICE behaviour — payload shapes, the monotonic
guard, the command-channel rules — not paho.

The point of the twin is that the backend cannot tell it from real hardware, so
these tests assert the contract in
`docs/hardware-firmware-requirements-v1.md` rather than the twin's internals.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SIMULATOR_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(SIMULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_DIR))

import twin  # noqa: E402


class RecordingClient:
    """Stands in for the paho client: records publishes, no network."""

    def __init__(self):
        self.published = []      # (topic, payload, qos, retain)
        self.subscriptions = []
        self.wills = []
        self.username = None

    def username_pw_set(self, user, password=None):
        self.username = (user, password)

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.wills.append((topic, payload, qos, retain))

    def subscribe(self, topics, qos=0):
        if isinstance(topimes := topics, list):
            self.subscriptions.extend(topices)
        else:
            self.subscriptions.append((topics, qos))

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

        class _Info:
            rc = 0

            def wait_for_publish(self, timeout=None):
                return None

            def is_published(self):
                return True

        return _Info()

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def connect(self, *args, **kwargs):
        pass


def make_twin(**kwargs):
    t = twin.DigitalTwin("GW-TWIN-01", "pw", **kwargs)
    t._client = RecordingClient()
    return t


def last_payload(recorder, topic_contains):
    for topic, payload, _qos, _retain in reversed(recorder.published):
        if topic_contains in topic:
            return json.loads(payload)
    return None


def all_payloads(recorder, topic_contains):
    return [json.loads(p) for topic, payload, _q, _r in recorder.published
            if topic_contains in topic]


# --------------------------------------------------------------------- topics

@pytest.mark.parametrize("unit_id,expected", [
    ("GW-1", "devices/GW-1/desired"),
    ("GW-9f8c2d34", "devices/GW-9f8c2d34/desired"),
])
def test_device_lane_topics_match_the_contract(unit_id, expected):
    assert twin.desired_topic(unit_id) == expected
    assert twin.command_topic(unit_id) == f"devices/{unit_id}/command"
    assert twin.reported_topic(unit_id) == f"devices/{unit_id}/reported"
    assert twin.command_response_topic(unit_id) == f"devices/{unit_id}/command/response"


def test_tenant_topic_shape():
    assert twin.tenant_topic("org-uuid", 12, "GW-1", "telemetry") == \
        "org-uuid/farms/12/gateways/GW-1/telemetry"


def test_hardware_id_follows_the_fleet_convention():
    """The pack resolver keys on (org, hardware_id, pack_id); the fleet uses
    "HW-" + the slot suffix, and the existing mappings were built that way."""
    assert make_twin().hardware_id == "HW-TWIN-01"
    assert twin.DigitalTwin("GW-ABC", "pw", hardware_id="HW-CUSTOM").hardware_id == "HW-CUSTOM"


# ------------------------------------------------------------------- telemetry

def test_telemetry_payload_is_schema_2_0_and_self_consistent():
    t = make_twin()
    t.org_id, t.tenant_farm_id, t.applied_version = "org-uuid", 7, 4
    body = t._telemetry_payload()

    # Gate 3: the pipeline rejects anything that is not 2.0.
    assert body["schema_version"] == "2.0"
    # Gate 2: the mirrored slot must equal the topic's slot.
    assert body["gateway_slot_id"] == t.unit_id
    # Gate 5: the pack resolver needs the hardware id.
    assert body["gateway_hardware_id"] == t.hardware_id
    # Gate 4: one top-level device_ts, and it is the dedup component.
    assert body["device_ts"]
    assert len(body["readings"]) == len(t.packs)
    for reading, pack in zip(body["readings"], t.packs):
        assert reading["pack_id"] == pack
        assert set(reading["metrics"]) == set(twin.METRIC_BASE)


def test_telemetry_is_unique_per_window():
    """The dedup key includes device_ts, so two windows in the same second must
    still differ or the second is silently dropped as a duplicate."""
    t = make_twin()
    a, b = t._telemetry_payload(), t._telemetry_payload()
    assert a["device_ts"] != b["device_ts"]


def test_metrics_stay_physically_plausible():
    t = make_twin(seed=1.0)
    for _ in range(200):
        for name, value in t._metrics(0).items():
            lo, hi = twin.METRIC_RANGE[name]
            assert lo <= value <= hi, f"{name}={value} outside [{lo},{hi}]"


def test_an_unbound_device_publishes_no_telemetry_or_status():
    """No org means no tenant topic — publishing anyway would be denied."""
    t = make_twin()
    assert t.tick() is False
    assert t.publish_status("ONLINE") is False
    assert t._client.published == []


# --------------------------------------------------------------- config apply

def _desired(version, slot="GW-TWIN-01", **extra):
    doc = {"schema_version": "1.0", "slot_id": slot, "config_version": version,
           "org_id": "org-uuid", "farm_id": 7, "sensors": [{"pack_id": "P-A"}],
           "report_interval_sec": 30}
    doc.update(extra)
    return json.dumps(doc).encode()


def test_applying_a_document_reports_applied_and_learns_the_tenant():
    t = make_twin()
    t._on_desired(_desired(3))
    assert t.applied_version == 3
    assert t.state == "CONFIGURED"
    assert t.org_id == "org-uuid"
    assert t.tenant_farm_id == 7
    assert t.report_interval_sec == 30.0
    report = t.reports[-1]
    assert report["state"] == "APPLIED"
    assert report["config_version"] == 3
    assert twin.reported_topic(t.unit_id) in [p[0] for p in t._client.published]


def test_an_older_or_equal_document_is_refused():
    """§3.3.2 — the monotonic guard. Applying a stale document would let a
    redelivery roll a fleet back."""
    t = make_twin()
    t._on_desired(_desired(5))
    t._on_desired(_desired(5))
    t._on_desired(_desired(4))
    assert t.applied_version == 5
    assert len(t.reports) == 1
    assert ("STALE", "5 <= 5") in t.faults_hit


def test_a_document_addressed_to_another_device_is_refused():
    t = make_twin()
    t._on_desired(_desired(9, slot="GW-SOMEONE-ELSE"))
    assert t.applied_version == 0
    assert t.faults_hit and t.faults_hit[-1][0] == "FOREIGN_SLOT"


def test_malformed_json_is_refused_without_crashing():
    t = make_twin()
    t._on_desired(b"{not json")
    assert t.applied_version == 0
    assert t.faults_hit[-1][0] == "MALFORMED_DOC"


# ----------------------------------------------------------- command channel

def _command(action="REFRESH_CONFIG", uuid="cmd-1", expires_in=300, **params):
    envelope = {"schema_version": "1.0", "command_uuid": uuid, "action": action,
                "params": params,
                "expires_at": (datetime.now(timezone.utc)
                               + timedelta(seconds=expires_in)).isoformat()}
    return json.dumps(envelope).encode()


def test_a_command_is_answered_in_three_separate_events():
    """§3.6 rule 2 — ACK, then act, then SUCCESS. Collapsing them would hide
    from the server that the device accepted the envelope."""
    t = make_twin()
    t._on_command(_command(), lane="device")
    states = [b["state"] for b in t.commands_seen]
    assert states == ["ACK", "SUCCESS"]
    assert all(b["command_uuid"] == "cmd-1" for b in t.commands_seen)
    assert t.commands_seen[-1]["result"]["applied_config_version"] == 0


def test_an_expired_command_is_answered_not_ignored():
    """§3.6 rule 1 — silence would leave the server's journal in SENT until its
    own sweep expires it, which is the stalled-command bug this channel fixed."""
    t = make_twin()
    t._on_command(_command(expires_in=-60), lane="device")
    assert [b["state"] for b in t.commands_seen] == ["EXPIRED"]
    assert t.commands == [("EXPIRED", "cmd-1")]


def test_an_expires_at_without_an_offset_is_read_as_utc():
    """The bug that made every command look 3.5 hours expired on a +03:30 box."""
    t = make_twin()
    naive = (datetime.now(timezone.utc) + timedelta(seconds=120)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    envelope = json.dumps({"command_uuid": "c", "action": "REFRESH_CONFIG",
                           "expires_at": naive}).encode()
    t._on_command(envelope, lane="device")
    assert [b["state"] for b in t.commands_seen] == ["ACK", "SUCCESS"]


def test_an_unknown_action_is_a_failed_response_not_a_silent_drop():
    t = make_twin()
    t._on_command(_command(action="LAUNCH_MISSILE"), lane="device")
    assert t.commands_seen[-1]["state"] == "FAILED"
    assert "unknown action" in t.commands_seen[-1]["error"]


def test_a_reboot_command_reports_the_count():
    t = make_twin()
    t._on_command(_command(action="REBOOT"), lane="device")
    t._on_command(_command(action="REBOOT", uuid="cmd-2"), lane="device")
    assert [b["result"]["reboots"] for b in t.commands_seen if b["state"] == "SUCCESS"] == [1, 2]


def test_rotation_proves_the_new_password_and_defers_the_reconnect():
    """§3.6 rule 5 — the SUCCESS body carries the password because that is the
    proof the server waits for, and the session is NOT dropped."""
    t = make_twin()
    t._on_command(_command(action="ROTATE_CREDENTIAL", new_password="new-secret"),
                  lane="device")
    success = [b for b in t.commands_seen if b["state"] == "SUCCESS"][-1]
    assert success["result"]["password"] == "new-secret"
    assert success["result"]["reconnect"] == "deferred"
    assert t.password == "new-secret"


def test_rotation_without_a_password_fails_loudly():
    t = make_twin()
    t._on_command(_command(action="ROTATE_CREDENTIAL"), lane="device")
    assert t.commands_seen[-1]["state"] == "FAILED"
    assert t.password == "pw"


def test_the_tenant_command_lane_is_used_after_binding():
    """§3.6 — the same channel moves to the tenant topics once bound."""
    t = make_twin()
    t._on_desired(_desired(1))
    t._client.published.clear()
    t._on_command(_command(uuid="tenant-cmd"), lane="tenant")
    topics = [p[0] for p in t._client.published]
    assert all("gateways/GW-TWIN-01/commands/response" in topic for topic in topics)


# --------------------------------------------------------------------- faults

def test_a_faulty_sensor_produces_an_out_of_range_reading():
    """Invalid data must reach the backend so its validation is exercised —
    silently 'fixing' it would hide the very path we want to test."""
    t = make_twin(faults=twin.FaultProfile(invalid_reading=1.0), seed=2.0)
    t.org_id, t.tenant_farm_id = "org", 1
    body = t._telemetry_payload()
    bad = [r for r in body["readings"]
           if any(v > hi for v in r["metrics"].values()
                  for lo, hi in [twin.METRIC_RANGE[
                      [k for k, v in r["metrics"].items() if v > twin.METRIC_RANGE[k][1]][0]]])]
    assert bad, "expected at least one out-of-range reading"
    assert any(f[0] == "INVALID" for f in t.faults_hit)


def test_a_dropped_window_is_skipped_not_sent():
    t = make_twin(faults=twin.FaultProfile(drop_connectivity=1.0), seed=3.0)
    t.org_id, t.tenant_farm_id = "org", 1
    assert t.tick() is False
    assert t.telemetry_sent == 0
    assert t.faults_hit[-1][0] == "DROP"


def test_the_fault_profile_reads_itself_from_the_environment():
    env = {"SIM_FAULT_DROP": "0.1", "SIM_FAULT_INVALID": "0.05",
           "SIM_FAULT_RESTART": "0", "SIM_FAULT_DELAY_SEC": "7"}
    f = twin.FaultProfile.from_env(env)
    assert (f.drop_connectivity, f.invalid_reading, f.restart) == (0.1, 0.05, 0.0)
    assert f.delay_seconds == 7.0
    assert f.describe() == "drop=0.1, invalid=0.05"
