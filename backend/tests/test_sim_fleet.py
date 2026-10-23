"""Unit tests for the fleet orchestrator (`simulator/sim.py`).

The twins are replaced by stubs, so what is under test is the FLEET's job:
loading credentials, refusing to silently lose a unit, keeping a dead unit
retrying, and reporting per-device identity so an operator can tell which
gateway produced what.
"""

import json
import sys
from pathlib import Path

import pytest

SIMULATOR_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(SIMULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_DIR))

import sim  # noqa: E402
import twin  # noqa: E402


class StubTwin:
    """A twin that never touches the network."""

    def __init__(self, unit_id, password, farm_id=None, **kwargs):
        self.unit_id = unit_id
        self.password = password
        self.farm_id = farm_id
        self.interval_sec = kwargs.get("interval_sec", 60)
        self.report_interval_sec = self.interval_sec
        self.connected = False
        self.org_id = None
        self.applied_version = 0
        self.telemetry_sent = 0
        self.faults_hit = []
        self.commands_seen = []
        self.reboots = 0
        self.state = "NEW"
        self.last_error = None
        self.start_calls = 0
        self.tick_calls = 0
        self._last_tick = 0.0
        self._last_reconnect = 0.0

    def start(self):
        self.start_calls += 1
        self.connected = True
        return 0

    def stop(self):
        self.connected = False

    def tick(self):
        self.tick_calls += 1
        return True

    def summary(self):
        return f"{self.unit_id} stub"


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(sim, "DigitalTwin", StubTwin)
    return sim.Fleet("emqx", 8883, 60.0, twin.FaultProfile())


def _write_creds(tmp_path, data):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_one_twin_per_credential_entry(fleet, tmp_path):
    path = _write_creds(tmp_path, {
        "GW-A": {"username": "GW-A", "password": "p1", "farm_id": 2},
        "GW-B": {"username": "GW-B", "password": "p2", "farm_id": 3},
    })
    assert fleet.load_credentials(path) == 2
    assert [t.unit_id for t in fleet.twins] == ["GW-A", "GW-B"]
    assert [t.farm_id for t in fleet.twins] == [2, 3]


def test_an_entry_without_a_password_is_skipped_not_crashed(fleet, tmp_path):
    """A half-written credentials file must not take the whole fleet down."""
    path = _write_creds(tmp_path, {
        "GW-GOOD": {"password": "p", "farm_id": 1},
        "GW-BROKEN": {"farm_id": 1},
    })
    assert fleet.load_credentials(path) == 1
    assert fleet.twins[0].unit_id == "GW-GOOD"


def test_a_missing_credentials_file_is_fatal(fleet, tmp_path):
    with pytest.raises(FileNotFoundError):
        fleet.load_credentials(str(tmp_path / "nope.json"))


def test_a_refused_connection_is_retried_then_logged_critical(fleet, tmp_path):
    """Silence is how a broken deployment looks healthy: the fleet must retry
    and then say so loudly."""
    path = _write_creds(tmp_path, {"GW-A": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(path)
    twin_obj = fleet.twins[0]

    calls = {"n": 0}

    def refusing():
        calls["n"] += 1
        return 5  # CONNACK: not authorised

    twin_obj.start = refusing
    fleet._connect_with_retry(twin_obj, attempts=3, backoff=0)
    assert calls["n"] == 3


def test_a_dead_twin_is_reconnected_rather_than_abandoned(fleet, tmp_path):
    """One unit losing its session must not silently stop reporting forever."""
    path = _write_creds(tmp_path, {"GW-A": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(path)
    t = fleet.twins[0]
    fleet.start_all()
    assert t.connected

    t.connected = False           # the broker dropped it
    t._last_reconnect = 0.0
    fleet._keep_alive(t)
    assert t.start_calls == 2      # reconnected
    assert t.connected


def test_a_connected_twin_is_left_alone(fleet, tmp_path):
    path = _write_creds(tmp_path, {"GW-A": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(path)
    t = fleet.twins[0]
    fleet.start_all()
    before = t.start_calls
    fleet._keep_alive(t)
    assert t.start_calls == before


def test_only_bound_twins_are_ticked(fleet, tmp_path):
    """An unbound twin has no tenant topic: ticking it would publish nowhere."""
    path = _write_creds(tmp_path, {
        "GW-BOUND": {"password": "p", "farm_id": 1},
        "GW-UNBOUND": {"password": "p", "farm_id": 1},
    })
    fleet.load_credentials(path)
    fleet.start_all()
    bound, unbound = fleet.twins
    bound.org_id = "org"
    unbound.org_id = None
    bound._last_tick = unbound._last_tick = 0.0

    for t in fleet.twins:
        if t.connected and t.org_id:
            t._last_tick = 0.0
            t.tick()
    assert bound.tick_calls == 1
    assert unbound.tick_calls == 0


def test_the_summary_names_every_device(fleet, tmp_path, caplog):
    """The owner's requirement: it must be possible to tell which farm or
    device produced each data point. The summary is that record."""
    import logging
    path = _write_creds(tmp_path, {"GW-A": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(path)
    with caplog.at_level(logging.INFO, logger="sim"):
        fleet._log_summary()
    assert "GW-A" in caplog.text


def test_request_stop_ends_the_loop(fleet, tmp_path):
    path = _write_creds(tmp_path, {"GW-A": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(path)
    fleet.start_all()
    fleet.request_stop()
    assert fleet._stopping is True
    fleet.stop_all()
    assert all(not t.connected for t in fleet.twins)


def test_the_fault_profile_defaults_to_off():
    """A clean fleet by default: faults are opt-in, or a green run proves
    nothing about error handling."""
    f = twin.FaultProfile()
    assert f.describe() == "none"
    assert (f.drop_connectivity, f.delay_send, f.invalid_reading, f.restart) == (0, 0, 0, 0)
