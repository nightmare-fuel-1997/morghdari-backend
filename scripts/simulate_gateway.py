#!/usr/bin/env python
"""
simulate_gateway.py — honest schema-2.0 gateway simulator (TASKS M3-08).

فارسی: شبیه‌ساز گیت‌وی — محموله‌های واقعیِ اسکیما-۲٫۰ با حالتِ طوفانِ پخشِ مجدد.

What it does (v0.3):
  1. Builds Gateway Firmware Spec payloads: ONE top-level device_ts per 60s
     window, is_buffered lane flag, multi-pack readings with metrics maps.
  2. --replay-hours N first publishes N hours of BUFFERED payloads with OLD
     timestamps (an outage rehearsal) at the real wire cadence, then resumes
     live publishing.
  3. Publishes qos=1 to {org}/farms/{farm}/gateways/{gateway}/telemetry on the
     dev EMQX. Credentials via MQTT_USERNAME/MQTT_PASSWORD env (dev user is
     created in the EMQX dashboard — never hardcode).

Pure builders (build_payload, replay_windows) are import-safe for tests;
paho stays lazily imported inside main().
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simulate_gateway",
        description=(
            "Morghdari gateway simulator — publishes Gateway Firmware Spec "
            "schema-2.0 payloads to the farm telemetry topic."
        ),
    )
    parser.add_argument("--gateway", required=True,
                        help="Gateway slot id, e.g. FARM-01-GW-A (topic segment)")
    parser.add_argument("--org", required=True, help="Org UUID (first topic segment)")
    parser.add_argument("--farm", default="1", help="Farm id topic segment (default: 1)")
    parser.add_argument("--hardware", dest="hardware_id",
                        default=None, help="gateway_hardware_id (default: derived from slot)")
    parser.add_argument("--packs", default="P-A",
                        help="Comma-separated pack ids simulated (default: P-A)")
    parser.add_argument("--replay-hours", type=int, default=0, dest="replay_hours",
                        help="Hours of buffered outage-replay BEFORE going live")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="Real seconds between publishes (default: 60; use small values for demos)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    return parser


def build_payload(
    hardware_id: str,
    gateway_slot: str,
    packs: list[str],
    device_ts: datetime,
    buffered: bool = False,
    clock_drift: bool = False,
    seq: int = 0,
) -> dict:
    """One schema-2.0 payload for ONE 60s window (Firmware Spec §payload).

    Every field is load-bearing: single top-level device_ts, is_buffered picks
    the lane, pack_id drives backend slot resolution, metrics values vary by
    `seq` so consecutive windows are visibly different in the API.
    فارسی: ساختِ محمولهٔ استاندارد برای یک پنجرهٔ ۶۰ ثانیه‌ای با چند پک.
    """
    readings = []
    for i, pack_id in enumerate(packs):
        base = 26.0 + i * 3 + (seq % 7) * 0.4
        readings.append({
            "pack_id": pack_id,
            "metrics": {
                "temp": {"avg": round(base, 1), "min": round(base - 1.5, 1), "max": round(base + 1.2, 1)},
                "nh3": {"avg": round(12 + i * 2 + (seq % 5), 1),
                         "min": 10.0, "max": 20.0, "max_ts": (seq * 7) % 60},
            },
        })
    return {
        "gateway_hardware_id": hardware_id,
        "gateway_slot_id": gateway_slot,
        "schema_version": "2.0",
        "device_ts": device_ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "is_buffered": buffered,
        "clock_drift": clock_drift,
        "readings": readings,
    }


def replay_windows(replay_hours: int, now: datetime | None = None):
    """Yield (device_ts, is_buffered) windows: N hours of buffered past,
    oldest first, then hand off to live. One window = one 60s step."""
    now = now or datetime.now(timezone.utc)
    total = max(replay_hours, 0) * 60
    start = (now - timedelta(minutes=total)).replace(second=0, microsecond=0)
    for offset in range(total):
        yield start + timedelta(minutes=offset), True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("paho-mqtt is required to publish: pip install paho-mqtt")
        return 1

    hardware_id = args.hardware_id or f"{args.gateway}-HW"
    packs = [p.strip() for p in args.packs.split(",") if p.strip()]
    topic = f"{args.org}/farms/{args.farm}/gateways/{args.gateway}/telemetry"

    import os

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sim-{args.gateway}-{os.getpid()}",
    )
    username = os.environ.get("MQTT_USERNAME", "").strip()
    if username:
        client.username_pw_set(username, os.environ.get("MQTT_PASSWORD", ""))
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()

    def publish(window_ts: datetime, buffered: bool, seq: int) -> None:
        payload = build_payload(hardware_id, args.gateway, packs, window_ts, buffered, seq=seq)
        info = client.publish(topic, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=10)
        tag = "buffered" if buffered else "live"
        print(f"[{tag}] {payload['device_ts']} → {topic}")

    seq = 0
    for window_ts, _buffered in replay_windows(args.replay_hours):
        publish(window_ts, True, seq)
        seq += 1
        time.sleep(min(args.interval, 2.0))  # replay cadence compressed for demos

    try:
        while True:
            publish(datetime.now(timezone.utc).replace(microsecond=0), False, seq)
            seq += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("simulator stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
