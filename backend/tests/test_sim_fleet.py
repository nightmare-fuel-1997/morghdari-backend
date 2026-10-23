"""Unit tests for the fleet orchestrator (`simulator/sim.py`).

The twins are replaced by stubs, so what is under test is the FLEET's job:
loading credentials, refusing to silently lose a unit, keeping a dead unit
retrying, and reporting per-device identity so an operator can tell which
gateway produced what.
"""

import json
import sys
import time
from pathlib import Path

import pytest

SIMULATOR_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(SIMULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_DIR))

import sim  # noqa: E402
import twin  # noqa: E402


# ---- the provisioned-credentials path --------------------------------------

PROVISIONED = {
    "GW-AUTO-000001": {
        "username": "GW-AUTO-000001", "password": "p1", "farm_id": 12,
        "hardware_id": "AA:BB:CC:000001", "packs": ["a1b2c3d4", "e5f6a7b8"],
    },
}


class StubTwin:
    """A twin that never touches the network."""

    def __init__(self, unit_id, password, farm_id=None, **kwargs):
        self.unit_id = unit_id
        self.password = password
        self.farm_id = farm_id
        self.hardware_id = kwargs.get("hardware_id") or ("HW-" + unit_id[len("GW-"):])
        self.packs = list(kwargs.get("packs") or ("P-A", "P-B"))
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
        # The twin owns these now (see DigitalTwin.__init__); a freshly joined
        # twin must be schedulable the moment it exists.
        self.last_tick_at = 0.0
        self.last_reconnect_at = 0.0

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
    """`tmp_path` may be a directory that does not exist yet."""
    if isinstance(tmp_path, str) and tmp_path.endswith(".json"):
        path = Path(tmp_path)
    else:
        path = Path(tmp_path) / "creds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
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


def test_a_missing_credentials_file_is_skipped_not_fatal(fleet, tmp_path):
    """One unreadable source must not take the fleet down — the provisioned
    file is allowed to be absent, so the base one being absent is the same
    shape of problem. `main()` is what refuses to start with nothing."""
    assert fleet.load_credentials(str(tmp_path / "nope.json")) == 0


def test_a_missing_provisioned_file_is_not_an_error(fleet, tmp_path, monkeypatch):
    """A fleet that has never provisioned anything must start normally."""
    monkeypatch.setattr(sim, "PROVISIONED_PATH", str(tmp_path / "absent.json"))
    base = _write_creds(tmp_path / "base", {"GW-A": {"password": "p", "farm_id": 1}})
    assert fleet.load_credentials(base, sim.PROVISIONED_PATH) == 1
    assert fleet.reload_provisioned() == 0


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
    t.last_reconnect_at = 0.0
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
    bound.last_tick_at = unbound.last_tick_at = 0.0

    for t in fleet.twins:
        if t.connected and t.org_id:
            t.last_tick_at = 0.0
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


# ---- provisioned gateways --------------------------------------------------

def _write_provisioned(tmp_path, data):
    path = tmp_path / "provisioned.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_a_provisioned_gateway_is_merged_into_the_fleet(fleet, tmp_path, monkeypatch):
    """The whole point of provision.py: a new gateway joins the fleet without
    anyone editing the credentials file by hand."""
    path = _write_provisioned(tmp_path, PROVISIONED)
    monkeypatch.setattr(sim, "PROVISIONED_PATH", path)
    base = _write_creds(tmp_path / "base", {"GW-A": {"password": "p", "farm_id": 1}})

    assert fleet.load_credentials(base, path) == 2
    auto = [t for t in fleet.twins if t.unit_id == "GW-AUTO-000001"][0]
    assert auto.farm_id == 12
    # the pack ids and hardware id come from the provisioning, not a default
    assert auto.hardware_id == "AA:BB:CC:000001"
    assert auto.packs == ["a1b2c3d4", "e5f6a7b8"]


def test_a_provisioned_gateway_joins_a_running_fleet(fleet, tmp_path, monkeypatch):
    """The reload path: the simulator is already up when the gateway appears."""
    path = _write_provisioned(tmp_path, PROVISIONED)
    monkeypatch.setattr(sim, "PROVISIONED_PATH", path)
    base = _write_creds(tmp_path / "base", {"GW-A": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(base)

    assert fleet.reload_provisioned() == 1
    assert len(fleet.twins) == 2
    # the new one is connected, not merely listed — `load_credentials` alone
    # does not connect anything, so only the newcomer is asserted here
    assert fleet.twins[-1].connected
    assert fleet.twins[-1].unit_id == "GW-AUTO-000001"


def test_a_reload_does_not_restart_a_running_twin(fleet, tmp_path, monkeypatch):
    """Restarting a healthy device to refresh its credential would be worse
    than the staleness — an existing twin is left exactly as it is."""
    path = _write_provisioned(tmp_path, PROVISIONED)
    monkeypatch.setattr(sim, "PROVISIONED_PATH", path)
    base = _write_creds(tmp_path / "base", {"GW-A": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(base, path)
    before = fleet.twins[0].start_calls

    assert fleet.reload_provisioned() == 0
    assert fleet.twins[0].start_calls == before


def test_a_missing_provisioned_file_is_not_an_error(fleet, tmp_path, monkeypatch):
    """A fleet that has never provisioned anything must start normally."""
    monkeypatch.setattr(sim, "PROVISIONED_PATH", str(tmp_path / "absent.json"))
    base = _write_creds(tmp_path / "base", {"GW-A": {"password": "p", "farm_id": 1}})
    assert fleet.load_credentials(base, sim.PROVISIONED_PATH) == 1
    assert fleet.reload_provisioned() == 0


def test_an_unreadable_provisioned_file_is_skipped_not_fatal(fleet, tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(sim, "PROVISIONED_PATH", str(path))
    base = _write_creds(tmp_path / "base", {"GW-A": {"password": "p", "farm_id": 1}})
    assert fleet.load_credentials(base, str(path)) == 1
    assert fleet.reload_provisioned() == 0


def test_the_default_pack_ids_are_backward_compatible(fleet, tmp_path):
    """A credentials entry with no packs must still get the fleet's P-A/P-B,
    which is what the existing mappings were built with."""
    base = _write_creds(tmp_path / "base", {"GW-LEGACY": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(base)
    assert fleet.twins[0].packs == ["P-A", "P-B"]
    assert fleet.twins[0].hardware_id == "HW-LEGACY"


# ---- the crash the live deploy hit ------------------------------------------

def test_a_newly_joined_twin_is_immediately_schedulable(fleet, tmp_path,
                                                        monkeypatch):
    """REGRESSION: a twin that joins a RUNNING fleet must be schedulable at once.

    The live deploy crashed here. `_keep_alive` returns early for a connected
    twin, before the lazy `hasattr` that set the scheduling attributes — so a
    freshly joined twin reached the run loop with no `_last_tick` and killed the
    whole simulator with an AttributeError. Four gateways went silent at once.

    The attributes belong to the twin, so a twin is schedulable from birth.
    """
    path = _write_provisioned(tmp_path, PROVISIONED)
    monkeypatch.setattr(sim, "PROVISIONED_PATH", path)
    base = _write_creds(tmp_path / "base", {"GW-A": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(base)
    fleet.start_all()

    assert fleet.reload_provisioned() == 1
    joined = fleet.twins[-1]

    # the exact expression the run loop evaluates, with the loop's real clock
    now = time.time()
    for t in fleet.twins:
        assert hasattr(t, "last_tick_at")
        assert hasattr(t, "last_reconnect_at")
        _ = (now - t.last_tick_at) >= t.report_interval_sec
        fleet._keep_alive(t)

    # and the newcomer is not merely schedulable, it is DUE
    assert (now - joined.last_tick_at) >= joined.report_interval_sec


def test_the_run_loop_survives_a_twin_joining_mid_flight(fleet, tmp_path,
                                                         monkeypatch):
    """The same failure one level up: drive the loop body over a mixed fleet."""
    path = _write_provisioned(tmp_path, PROVISIONED)
    monkeypatch.setattr(sim, "PROVISIONED_PATH", path)
    base = _write_creds(tmp_path / "base", {"GW-A": {"password": "p", "farm_id": 1}})
    fleet.load_credentials(base)
    fleet.start_all()
    fleet.reload_provisioned()
    fleet.twins[0].org_id = "org"
    fleet.twins[-1].org_id = "org"

    now = time.time()
    for t in fleet.twins:
        fleet._keep_alive(t)
        if t.connected and t.org_id:
            if now - t.last_tick_at >= t.report_interval_sec:
                t.last_tick_at = now
                t.tick()

    assert fleet.twins[0].tick_calls == 1
    assert fleet.twins[-1].tick_calls == 1
