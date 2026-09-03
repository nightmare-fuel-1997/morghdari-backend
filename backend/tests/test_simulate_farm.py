"""simulate_farm.py pure builders — value realism, determinism, scenarios.

The MQTT/paho half is deliberately NOT tested here (needs a broker); only the
import-safe pure functions are pinned, so a broken math change can never ship
with a green suite (same philosophy as test_m3_08_simulator.py).
فارسی: تستِ توابعِ خالصِ شبیه‌ساز مزرعه — واقع‌گرایی اعداد، قطعیت و سناریوها.
"""

import importlib.util
import json
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SIM_PATH = Path(__file__).resolve().parents[2] / "scripts" / "simulate_farm.py"
if not SIM_PATH.exists():
    pytest.skip("simulate_farm.py not packaged in CI image", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("simulate_farm", SIM_PATH)
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

SCENARIO = {
    "slot_breach_windows": sim.SLOT_BREACH_WINDOWS,
    "velocity_windows": sim.VELOCITY_WINDOWS,
    "temp_breach_windows": sim.TEMP_BREACH_WINDOWS,
    "los_windows": sim.LOS_WINDOWS,
    "correlation_windows": sim.CORRELATION_WINDOWS,
    "replay_windows": sim.REPLAY_WINDOWS,
    "thi_windows": sim.THI_WINDOWS,
}


def test_build_payload_shape():
    ts = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
    payload = sim.build_payload(
        "FARM-01-GW-A-HW", "S01-PACK-A", "P-A", ts,
        {"temp": {"avg": 27.5}},
    )
    assert payload["schema_version"] == "2.0"
    assert payload["gateway_hardware_id"] == "FARM-01-GW-A-HW"
    assert payload["gateway_slot_id"] == "S01-PACK-A"
    assert payload["device_ts"] == "2026-08-30T12:00:00Z"
    assert payload["is_buffered"] is False
    assert payload["clock_drift"] is False
    assert len(payload["readings"]) == 1
    assert payload["readings"][0]["pack_id"] == "P-A"
    assert payload["readings"][0]["metrics"] == {"temp": {"avg": 27.5}}


def test_value_at_is_deterministic():
    a = sim.value_at(0, 0, 100, SCENARIO)
    b = sim.value_at(0, 0, 100, SCENARIO)
    assert a == b


def test_value_at_ranges_are_realistic():
    """Temp stays near the base ± diurnal+noise; NH3 positive; humidity sane."""
    for window in range(0, 1440, 60):  # a few hours apart across the day
        for saloon in range(3):
            for pack in range(2):
                m = sim.value_at(saloon, pack, window, SCENARIO)
                temp = m["temp"]["avg"]
                nh3 = m["nh3"]["avg"]
                hum = m["humidity"]["avg"]
                assert 20.0 <= temp <= 38.0   # breaches push temp to ~36
                assert 0.0 <= nh3 <= 80.0
                assert 0.0 <= hum <= 100.0
                # min <= avg <= max for temp/nh3
                assert m["temp"]["min"] <= m["temp"]["avg"] <= m["temp"]["max"]
                assert m["nh3"]["min"] <= m["nh3"]["avg"] <= m["nh3"]["max"]


def test_breach_scenario_raises_nh3_and_recovers():
    """Saloon 0 pack A: drift starts at window 24 (sustained, +14 → over the
    25 threshold) and the velocity spike lands at window 10 (max_ts=59).
    Baseline must show a clear jump past threshold, and the breach must STOP
    after its end window (recovery)."""
    before = sim.value_at(0, 0, 20, SCENARIO)   # pre-breach baseline
    during = sim.value_at(0, 0, 30, SCENARIO)   # inside 24..48, fully ramped
    assert during["nh3"]["avg"] > before["nh3"]["avg"] + 10.0
    assert during["nh3"]["avg"] > 25.0        # crosses the NH3 threshold

    after = sim.value_at(0, 0, 60, SCENARIO)    # past end (48) → recovered
    assert after["nh3"]["avg"] < during["nh3"]["avg"] - 10.0

    # velocity window: single-window max_ts spike (max_ts=59) + value jump
    spike = sim.value_at(0, 0, 10, SCENARIO)
    normal = sim.value_at(0, 0, 11, SCENARIO)
    assert spike["nh3"]["max_ts"] == 59
    assert normal["nh3"]["max_ts"] == 0
    assert spike["nh3"]["avg"] > normal["nh3"]["avg"] + 25.0

    # calm scenario applies no drifts / no velocity at all
    calm = {"slot_breach_windows": {}, "velocity_windows": {}, "temp_breach_windows": {}}
    calm_spike = sim.value_at(0, 0, 10, calm)
    assert calm_spike["nh3"]["avg"] < 45.0
    assert calm_spike["nh3"]["max_ts"] == 0


def test_temp_breach_scenario_crosses_threshold_and_recovers():
    """Saloon 1 pack A: temp drift +6 during 35..60 pushes past the 30 °C
    threshold, then recovers."""
    before = sim.value_at(1, 0, 30, SCENARIO)
    during = sim.value_at(1, 0, 40, SCENARIO)
    assert during["temp"]["avg"] > before["temp"]["avg"] + 3.0
    assert during["temp"]["avg"] > 30.0
    after = sim.value_at(1, 0, 70, SCENARIO)
    assert after["temp"]["avg"] < during["temp"]["avg"] - 3.0


def test_scenario_factor():
    assert sim.scenario_factor(5, SCENARIO, 0) == 0.0
    assert sim.scenario_factor(25, SCENARIO, 0) == 1.0    # inside 24..48
    assert sim.scenario_factor(60, SCENARIO, 0) == 0.0    # past end
    assert sim.scenario_factor(25, {"slot_breach_windows": {}, "velocity_windows": {}}, 0) == 0.0


def test_los_windows_silence_devices():
    """LOS windows report silent for the scripted pack only, and the
    farm-wide correlation window silences every pack."""
    # saloon 0 pack A silent 160..172; pack B unaffected
    assert sim._in_los_window(0, 0, 165, SCENARIO) is True
    assert sim._in_los_window(0, 1, 165, SCENARIO) is False
    assert sim._in_los_window(0, 0, 175, SCENARIO) is False  # recovered

    # saloon 2 pack B silent 190..200
    assert sim._in_los_window(2, 1, 195, SCENARIO) is True
    assert sim._in_los_window(2, 0, 195, SCENARIO) is False

    # correlation window silences ALL packs of ALL saloons
    for saloon in range(3):
        for pack in range(2):
            assert sim._in_los_window(saloon, pack, 223, SCENARIO) is True
    assert sim._in_los_window(0, 0, 228, SCENARIO) is False

    # calm scenario: nothing silent
    calm = {"los_windows": {}, "correlation_windows": []}
    assert sim._in_los_window(0, 0, 165, calm) is False


def test_replay_windows_flag_buffered():
    """Replay windows flag the scripted pack for buffered publishing."""
    assert sim._in_replay_window(1, 0, 245, SCENARIO) is True
    assert sim._in_replay_window(1, 1, 245, SCENARIO) is False
    assert sim._in_replay_window(1, 0, 255, SCENARIO) is False  # past end

    calm = {"replay_windows": {}}
    assert sim._in_replay_window(1, 0, 245, calm) is False


def test_replay_device_ts_steps_back():
    """Buffered replay readings carry a stepped-back device_ts."""
    ts = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
    assert sim.replay_device_ts(ts) == datetime(2026, 8, 30, 11, 0, 0, tzinfo=timezone.utc)
    assert sim.replay_device_ts(ts, offset_minutes=30) == datetime(2026, 8, 30, 11, 30, 0, tzinfo=timezone.utc)


def test_thi_scenario_raises_temp_and_humidity_together():
    """THI windows raise BOTH inputs (co-presence requirement for the derived
    NRC index) and recover after the window ends. Saloon 2 pack A: 170..185."""
    before = sim.value_at(2, 0, 160, SCENARIO)
    during = sim.value_at(2, 0, 180, SCENARIO)
    assert during["temp"]["avg"] > before["temp"]["avg"] + 5.0
    assert during["humidity"]["avg"] > before["humidity"]["avg"] + 12.0

    after = sim.value_at(2, 0, 190, SCENARIO)
    assert after["temp"]["avg"] < during["temp"]["avg"] - 5.0

    # other pack unaffected
    other = sim.value_at(2, 1, 180, SCENARIO)
    assert other["temp"]["avg"] < during["temp"]["avg"] - 5.0

    # derived THI actually crosses the seeded thi_max=79 during the window
    from importlib import import_module
    capability = import_module("apps.alerts.services.capability")
    thi = capability.compute_thi(during["temp"]["avg"], during["humidity"]["avg"])
    assert thi > 79.0

    # calm scenario: no THI drift
    calm = {"thi_windows": {}}
    calm_during = sim.value_at(2, 0, 180, calm)
    assert calm_during["temp"]["avg"] < during["temp"]["avg"] - 5.0


def test_status_topic_mirrors_telemetry_with_status_leaf():
    """The pack-liveness topic shares the telemetry topic's org/farm/gateway
    segments exactly — same place a reader would look, /status leaf."""
    org, farm, slot = "org-1", 7, "S03-PACK-A"
    t = sim._topic(org, farm, slot)
    assert sim.status_topic(org, farm, slot) == t.replace("/telemetry", "/status")
    assert t.startswith(f"{org}/farms/{farm}/gateways/{slot}")
    assert sim.status_topic(org, farm, slot).endswith("/status")


def test_pack_status_payload_is_schema_shaped():
    """online/offline retained payloads carry the same identifying fields as
    telemetry (hardware, slot, pack) plus the status word."""
    online = sim.pack_status_payload("online", "FARM-03-GW-A-HW", "S03-PACK-A", "P-A")
    assert online["status"] == "online"
    assert online["schema_version"] == "2.0"
    assert online["gateway_hardware_id"] == "FARM-03-GW-A-HW"
    assert online["gateway_slot_id"] == "S03-PACK-A"
    assert online["pack_id"] == "P-A"
    assert online["ts"].endswith("Z")

    offline = sim.pack_status_payload("offline", "FARM-03-GW-A-HW", "S03-PACK-A", "P-A")
    assert offline["status"] == "offline"

    with pytest.raises(ValueError):
        sim.pack_status_payload("suspended", "hw", "slot", "P-A")


def test_manifest_slots_thread_each_pack_its_own_farm():
    """Every pack publishes under the farm its OWN saloon belongs to — not
    the manifest's top-level (first-farm) id, which used to oversell: a
    Morvarid pack claimed to live in Baharestan."""
    manifest = {
        "org_id": "org", "farm_id": 6,
        "saloons": [
            {"name": "A", "farm_id": 6, "slots": [{"pack": "P-A", "slot_id": "s1"}]},
            {"name": "B", "farm_id": 7, "slots": [{"pack": "P-A", "slot_id": "s2"}]},
            # a saloon without a farm segment falls back to the manifest's
            {"name": "C", "slots": [{"pack": "P-A", "slot_id": "s3"}]},
        ],
    }
    slots = sim._manifest_slots(manifest)
    assert [s[2]["farm_id"] for s in slots] == [6, 7, 6]
    assert [s[2]["saloon_index"] for s in slots] == [0, 1, 2]
    assert [s[1] for s in slots] == [0, 0, 0]

    # status topics follow the threaded farm, not the manifest default
    assert sim.status_topic("org", slots[1][2]["farm_id"], "s2") == "org/farms/7/gateways/s2/status"


def test_window_stepper_advances_one_minute():
    start = datetime(2026, 8, 30, 10, 0, 30, tzinfo=timezone.utc)
    steps = sim.window_stepper(start)
    first, idx0 = next(steps)
    second, idx1 = next(steps)
    assert first == datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)  # truncated
    assert idx0 == 0 and idx1 == 1
    assert second - first == timedelta(minutes=1)
    assert first.tzinfo is not None and second.tzinfo is not None    # start_index skips ahead: timestamps AND indices must jump together
    steps2 = sim.window_stepper(start, start_index=24)
    first2, idx2 = next(steps2)
    assert idx2 == 24
    assert first2 == datetime(2026, 8, 30, 10, 24, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Boot resilience — the MQTT half, tested with a MOCKED client (no broker):
# publish_loop lazily imports paho INSIDE the function, so a fake module in
# sys.modules lets us pin the boot retry/backoff, the retained LWT status
# publishes, and the CONNACK-failure logging branch — none of which a
# broker-less suite could otherwise touch.
# ---------------------------------------------------------------------------


def _fake_paho(monkeypatch, client):
    """Install a stub `paho.mqtt.client` whose Client() returns `client`.

    `import paho.mqtt.client as mqtt` resolves through the PACKAGE TREE's
    attributes (paho -> paho.mqtt -> paho.mqtt.client), not a bare leaf
    entry in sys.modules — so all three levels must be faked together.
    """
    fake_client = types.ModuleType("paho.mqtt.client")
    fake_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2="v2")
    fake_client.Client = lambda *a, **k: client
    fake_mqtt = types.ModuleType("paho.mqtt")
    fake_mqtt.client = fake_client
    fake_paho = types.ModuleType("paho")
    fake_paho.mqtt = fake_mqtt
    monkeypatch.setitem(sys.modules, "paho", fake_paho)
    monkeypatch.setitem(sys.modules, "paho.mqtt", fake_mqtt)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", fake_client)
    return fake_client


def _sim_args(**overrides):
    base = {
        "host": "localhost", "port": 1883,
        "mqtt_username_prefix": "demo-sim", "mqtt_password": "devpass",
        "dry_run": False, "start_window": 0,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


_SLOT = {
    "hardware_id": "FARM-01-GW-A-HW", "slot_id": "S01-PACK-A", "pack": "P-A",
    "saloon_index": 0, "farm_id": 6,
}


def _payload_status(published_args):
    """(topic, payload) positional args -> (topic, status_word). The ts field
    is wall-clock so exact bytes are never compared (qos/retain ride as
    kwargs and aren't part of args)."""
    topic, raw = published_args
    return topic, json.loads(raw)["status"]


def test_publish_loop_boot_retry_backs_off_then_connects(monkeypatch, capsys):
    """A broker down at boot must not kill the pack: connect raises OSError
    twice, the loop backs off 2s then 4s (2**attempt, capped at 15), and only
    then proceeds — retained online status, telemetry, clean-shutdown offline.
    The LWT itself is installed BEFORE the first connect attempt."""
    client = MagicMock()
    client.connect.side_effect = [OSError("connection refused"), OSError("connection refused"), None]
    _fake_paho(monkeypatch, client)
    sleeps = []
    monkeypatch.setattr(sim.time, "sleep", sleeps.append)

    stop = threading.Event()
    published_windows = []
    sim.publish_loop(
        _sim_args(), stop, "org-1", 6, dict(_SLOT), 0, SCENARIO, 0.05,
        on_publish=lambda _slot, window: (published_windows.append(window), stop.set()),
    )

    # boot backoff: two failures → 2s then 4s; the interval sleep after the
    # first successful publish is the 0.05s demo pace, not part of the backoff
    assert sleeps[:2] == [2, 4]
    assert client.connect.call_count == 3
    out = capsys.readouterr().out
    assert "broker unreachable (connection refused); retry in 2s" in out
    assert "broker unreachable (connection refused); retry in 4s" in out

    # credentials + the will were set BEFORE the first connect attempt; the
    # username is the PER-FARM identity (demo-sim-farm6) so EMQX ACLs can
    # pin this pack to its own farm
    client.username_pw_set.assert_called_once_with("demo-sim-farm6", "devpass")
    will_topic, will_raw = client.will_set.call_args.args
    assert client.will_set.call_args.kwargs == {"qos": 1, "retain": True}
    assert will_topic == sim.status_topic("org-1", 6, "S01-PACK-A")
    assert json.loads(will_raw)["status"] == "offline"

    # once connected: retained online status first, then real telemetry
    assert client.loop_start.called
    online_topic, online_status = _payload_status(client.publish.call_args_list[0].args)
    assert online_topic == will_topic and online_status == "online"
    assert client.publish.call_args_list[0].kwargs == {"qos": 1, "retain": True}
    telemetry_topic = client.publish.call_args_list[1].args[0]
    assert telemetry_topic == sim._topic("org-1", 6, "S01-PACK-A")
    assert published_windows == [0]

    # clean shutdown: the retained word flips back to offline BEFORE the
    # client is stopped/disconnected (the DISCONNECT suppresses the will)
    shutdown_topic, shutdown_status = _payload_status(client.publish.call_args_list[-1].args)
    assert shutdown_topic == will_topic and shutdown_status == "offline"
    assert client.loop_stop.called and client.disconnect.called


def test_publish_loop_logs_conack_failure_and_success(monkeypatch, capsys):
    """The on_connect callback reports broker rejections loudly (a silent
    re-CONNACK would stall the demo) and connects are visible with the
    session_present flag — both branches of the demo-sim log."""
    client = MagicMock()
    _fake_paho(monkeypatch, client)
    monkeypatch.setattr(sim.time, "sleep", lambda _s: None)

    # stop is pre-set so the loop returns before any connect attempt — but
    # the callback is assigned BEFORE the retry loop, so it's captured
    stop = threading.Event()
    stop.set()
    sim.publish_loop(_sim_args(), stop, "org-1", 6, dict(_SLOT), 0, SCENARIO, 0.05)
    client.connect.assert_not_called()

    class _Failure:
        is_failure = True

    class _Ok:
        is_failure = False

    client.on_connect(None, None, None, _Failure())
    out = capsys.readouterr().out
    assert "[sim:P-A] CONNACK failure" in out

    client.on_connect(None, None, types.SimpleNamespace(session_present=True), _Ok())
    out = capsys.readouterr().out
    assert "[sim:P-A] connected to broker (session_present=True)" in out

    client.on_connect(None, None, types.SimpleNamespace(session_present=False), _Ok())
    out = capsys.readouterr().out
    assert "[sim:P-A] connected to broker (session_present=False)" in out


def test_connect_args_use_per_farm_username():
    """The MQTT identity is farm-scoped: {prefix}-farm{farm_id} so the broker
    can authorize each pack to ITS OWN farm only. No prefix -> anonymous;
    no farm -> bare prefix."""
    conn = sim._connect_args(_sim_args(), 7)
    assert conn["username"] == "demo-sim-farm7"
    assert conn["password"] == "devpass"

    conn = sim._connect_args(_sim_args(), 6)
    assert conn["username"] == "demo-sim-farm6"  # never the bare prefix for a real farm

    conn = sim._connect_args(_sim_args(mqtt_username_prefix=None), 6)
    assert conn["username"] is None  # anonymous (dev/test broker without ACLs)

    conn = sim._connect_args(types.SimpleNamespace(host="h", port=1883, mqtt_password="p"), None)
    assert conn["username"] == "demo-sim"  # manifest without farms -> bare prefix
    assert conn["password"] == "p"
