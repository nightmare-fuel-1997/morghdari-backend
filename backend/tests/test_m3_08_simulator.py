"""M3-08 acceptance — simulator builders are schema-2.0 honest.

WHY: the simulator is the rehearsal bench for storms; its payloads must match
the Firmware Spec FIELD-FOR-FIELD (single top-level device_ts, is_buffered
lane flag, pack_id per reading) and its replay schedule must emit N hours of
buffered past BEFORE live. Pure functions — sqlite-safe by design.
فارسی: پذیرش M3-08 — سازنده‌های محموله و برنامهٔ پخشِ مجدد، دقیقاً مطابق سند.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SIM_PATH = Path(__file__).resolve().parents[2] / "scripts" / "simulate_gateway.py"

if not SIM_PATH.exists():
    # CI image packages only ./backend (Q3 broker-less decision family); the
    # simulator is exercised in the dev compose + recorded demo instead.
    pytest.skip("simulate_gateway.py not packaged in CI image", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("simulate_gateway", SIM_PATH)
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def test_build_payload_matches_firmware_spec_fields():
    ts = NOW - timedelta(minutes=1)
    payload = sim.build_payload(
        hardware_id="AA:BB", gateway_slot="FARM-01-GW-A",
        packs=["P-A", "P-B"], device_ts=ts, buffered=False, seq=3,
    )
    # Locked wire contract (schema-2.0): exact top-level keys, nothing invented.
    assert set(payload) == {
        "gateway_hardware_id", "gateway_slot_id", "schema_version",
        "device_ts", "is_buffered", "clock_drift", "readings",
    }
    assert payload["schema_version"] == "2.0"
    assert payload["gateway_slot_id"] == "FARM-01-GW-A"
    assert payload["device_ts"] == "2026-08-25T11:59:00Z"
    assert payload["is_buffered"] is False
    assert [r["pack_id"] for r in payload["readings"]] == ["P-A", "P-B"]
    for reading in payload["readings"]:
        assert set(reading["metrics"]["temp"]) == {"avg", "min", "max"}
        assert set(reading["metrics"]["nh3"]) == {"avg", "min", "max", "max_ts"}


def test_consecutive_windows_differ_visibly():
    a = sim.build_payload("HW", "GW", ["P-A"], NOW, seq=1)
    b = sim.build_payload("HW", "GW", ["P-A"], NOW + timedelta(seconds=60), seq=5)
    assert a["readings"][0]["metrics"] != b["readings"][0]["metrics"]


def test_replay_windows_emits_buffered_past_oldest_first():
    windows = list(sim.replay_windows(2, now=NOW))
    assert len(windows) == 120  # 2 hours × 60 windows/hour
    first_ts, first_buffered = windows[0]
    last_ts, last_buffered = windows[-1]
    assert first_buffered is True and last_buffered is True
    assert first_ts == NOW - timedelta(minutes=120)  # oldest FIRST
    assert last_ts == NOW - timedelta(minutes=1)


def test_zero_replay_hours_means_live_only():
    assert list(sim.replay_windows(0, now=NOW)) == []


def test_cli_parses_new_flags_without_paho():
    parser = sim.build_parser()
    args = parser.parse_args([
        "--gateway", "FARM-01-GW-A", "--org", "00000000-0000-0000-0000-000000000000",
        "--replay-hours", "12", "--interval", "0.5",
    ])
    assert args.replay_hours == 12
    assert args.interval == 0.5
