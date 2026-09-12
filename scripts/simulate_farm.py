#!/usr/bin/env python
"""simulate_farm.py — LIVE-demo farm simulator: fake gateways + fake devices,
publishing REAL schema-2.0 payloads over the REAL MQTT path (demo branches).

فارسی: شبیه‌ساز مزرعهٔ نمایشی — گیت‌وی‌ها و حسگرهای جعلی که دادهٔ واقعیِ
اسکیما-۲٫۰ را از مسیر واقعی MQTT به داشبورد زنده می‌فرستند.

What it does:
  1. Reads the topology manifest the operator supplies (farm/saloon ids, built
     through the API since contract v1.7.2: POST /api/v1/farms/, POST
     /api/v1/farms/{id}/saloons/) via `--manifest`.
  2. Spawns ONE paho MQTT client PER SENSOR PACK (each a fake "device"), each
     publishing its own schema-2.0 reading to the REAL broker every
     `--interval` seconds (default 5). device_ts advances in REAL 60-second
     windows so the charts fill once per wall-clock interval without ever
     tripping the per-window dedup guard.
  3. Values are realistic: a diurnal temperature sine per saloon, slow NH3
     drift, humidity tied to temp — plus SCRIPTED scenarios that exercise the
     full v0.4 alert brain: sustained breaches (Tier-1 at the 30 s demo pace),
     the instant NH3 velocity lane, single-slot LOS outages (watchdog fires,
     then auto-resolves), a farm-wide outage (GS-CORRELATION consolidated
     page), a buffered replay backlog (REPLAY_ANALYSIS rows, never pushed) and
     THI heat-stress windows (derived NRC index through the normal breach path).
  4. --dry-run prints the payloads instead of touching the broker.

Pure builders (value_at, scenario_factor, build_payload, window_stepper) are
import-safe for tests; paho stays lazily imported inside main().
"""

import argparse
import json
import math
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone


# --------------------------------------------------------------------------
# Scenario timeline (deterministic per saloon index so a showroom looks
# planned, not random). "Breach window index" = how many 60s device windows
# have elapsed since the simulator started.
#
#   slot_breach_windows : {saloon_index: [(start_window, end_window, pack_suffix, drift)]}
#       A pack drifts UP from its baseline by `drift` from start to end
#       (sustained breach window → Tier-1 after the demo pace), then recovers.
#   velocity_windows : {saloon_index: [window_index]}
#       max_ts set to 59 → INSTANT Tier-1 via the NH3 velocity lane (bypasses
#       the duration gate entirely) for ONE window only.
#   los_windows : {saloon_index: [(start_window, end_window, pack_suffix)]}
#       The device goes SILENT for the whole window (nothing is published) —
#       its sensor:status key expires, the LOS watchdog fires, then the device
#       "comes back" and the LOS row auto-resolves.
#   correlation_windows : [(start_window, end_window)]
#       EVERY pack of EVERY saloon falls silent together (farm-wide outage) —
#       ≥0.7 of a farm's ACTIVE slots LOS within 3 min → GS-CORRELATION
#       consolidated page instead of per-slot pages.
#   replay_windows : {saloon_index: [(start_window, end_window, pack_suffix)]}
#       The device publishes BUFFERED readings with stepped-back device_ts —
#       the historical lane drains the backlog, replay:pending hits 0 and the
#       replay sweep writes retroactive REPLAY_ANALYSIS rows (never pushed).
#   thi_windows : {saloon_index: [(start_window, end_window, pack_suffix, temp_add, hum_add)]}
#       Temp AND humidity rise together so the DERIVED THI heat-stress index
#       (NRC formula, needs co-present temp+humidity) crosses thi_max through
#       the normal breach machinery.
# --------------------------------------------------------------------------
SLOT_BREACH_WINDOWS = {
    # (start, end, pack_suffix, drift) — NH3 drift up past the 25 threshold:
    # baseline ~12 + 14 = ~26, clearly breaching, then recovers.
    0: [(24, 48, "A", 14.0), (80, 110, "B", 14.0)],
    1: [(48, 80, "B", 14.0)],
    2: [(60, 95, "A", 14.0), (120, 150, "B", 14.0)],
}
VELOCITY_WINDOWS = {
    # single-window max_ts=59 velocity spikes
    0: [10, 100],
    1: [30, 130],
    2: [20, 140],
}
# Temp breach windows: saloon 1 pack A +6 (baseline ~27 + diurnal up to ~30.5 ->
# crosses the 30 threshold briefly) so the temp chart also shows a crossing.
TEMP_BREACH_WINDOWS = {
    1: [(35, 60, "A", 6.0)],
}
# LOS windows: single slots drop out (saloon 0 pack A, then saloon 2 pack B) —
# one saloon-scoped Tier-1 LOS alert per outage episode, auto-resolving on
# return. Kept clear of the NH3/temp breach windows so the story reads cleanly.
LOS_WINDOWS = {
    0: [(160, 172, "A")],
    2: [(190, 200, "B")],
}
# Farm-wide outage: ALL packs of ALL saloons silent for windows 220..226 —
# crosses the 0.7 farm LOS ratio within 3 min → one consolidated page.
CORRELATION_WINDOWS = [(220, 226)]
# Replay windows: saloon 1 pack A replays an hour of buffered readings
# (device_ts stepped back 60 min) — backlog drains → REPLAY_ANALYSIS rows.
REPLAY_WINDOWS = {
    1: [(240, 250, "A")],
}
# THI heat-stress windows: temp +7 AND humidity +18 together. With T≈33.5, RH
# ≈76 the NRC index crosses the thi_max=79 threshold through the normal breach
# path. Saloon 2 pack A.
THI_WINDOWS = {
    2: [(170, 185, "A", 7.0, 18.0)],
}

# Diurnal amplitude around each saloon's baseline (colder at night, warm mid-day).
DIURNAL_AMP = 3.5
# Slow upward NH3 drift per window — charts keep moving between scenarios.
NH3_DRIFT_PER_WINDOW = 0.01
# Humidity roughly tracks temperature (higher in the cool of the night).
HUMIDITY_BASE = 58.0
# Baseline NH3 (ppm) far below the 25 threshold so breaches are scripted only.
BASE_NH3 = 12.0
# Baseline temp (°C) near but below the 30 threshold (diurnal + noise must
# stay under; only the drift scenario ever crosses it).
BASE_TEMP = 26.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _in_breach_window(saloon_index: int, pack_index: int, window_index: int, scenario: dict):
    """True when this pack is inside a scripted sustained-breach window."""
    pack_suffix = "AB"[pack_index]
    for (start, end, suffix, _drift) in scenario.get("slot_breach_windows", {}).get(saloon_index, []):
        if suffix == pack_suffix and start <= window_index <= end:
            return True
    return False


def _in_velocity_window(saloon_index: int, window_index: int, scenario: dict) -> bool:
    return window_index in scenario.get("velocity_windows", {}).get(saloon_index, [])


def value_at(
    saloon_index: int,
    pack_index: int,
    window_index: int,
    scenario: dict,
    base_temp: float = BASE_TEMP,
    base_nh3: float = BASE_NH3,
) -> dict:
    """One window's realistic temp/nh3/humidity metrics for a saloon/pack.

    Pure + deterministic: same (saloon, pack, window, scenario) always yields
    the same numbers, so tests can pin shapes and ranges without a broker.
    Baselines stay comfortably inside thresholds; only scripted scenarios
    (sustained drift or velocity spike) cross them.
    """
    phase = (saloon_index * 1.7) + (pack_index * 0.9)
    # 24h cycle in 60s windows → 1440 windows per full day.
    t = (window_index % 1440) / 1440.0
    diurnal = DIURNAL_AMP * math.sin(2 * math.pi * t + phase)

    temp = base_temp + diurnal + (pack_index * 0.6) + _jitter(window_index, saloon_index, pack_index, 0.25)
    nh3 = base_nh3 + NH3_DRIFT_PER_WINDOW * (window_index % 600) + _jitter(window_index, saloon_index, pack_index, 0.35)
    humidity = HUMIDITY_BASE - 0.35 * (temp - base_temp) + _jitter(window_index, saloon_index, pack_index, 1.5)

    # Scenario: sustained NH3 breach window (drift up, then recover)
    for (start, end, suffix, drift) in scenario.get("slot_breach_windows", {}).get(saloon_index, []):
        if suffix == "AB"[pack_index] and start <= window_index <= end:
            ramp = min(1.0, (window_index - start) / 4.0)  # reach full drift in 4 windows
            nh3 += drift * ramp

    # Scenario: sustained TEMP breach window (drift up, then recover)
    for (start, end, suffix, drift) in scenario.get("temp_breach_windows", {}).get(saloon_index, []):
        if suffix == "AB"[pack_index] and start <= window_index <= end:
            ramp = min(1.0, (window_index - start) / 4.0)
            temp += drift * ramp

    # Scenario: velocity spike — max_ts 59 (>= 50 threshold) + big value jump
    velocity = _in_velocity_window(saloon_index, window_index, scenario)
    if velocity:
        nh3 += 28.0  # dramatic spike so the alert strip shows a scary number

    # Scenario: THI heat stress — temp AND humidity rise together so the
    # derived NRC index crosses thi_max through the normal breach machinery.
    for (start, end, suffix, t_add, h_add) in scenario.get("thi_windows", {}).get(saloon_index, []):
        if suffix == "AB"[pack_index] and start <= window_index <= end:
            ramp = min(1.0, (window_index - start) / 4.0)
            temp += t_add * ramp
            humidity += h_add * ramp

    return {
        "temp": {"avg": round(temp, 1), "min": round(temp - 1.2, 1), "max": round(temp + 1.2, 1)},
        "nh3": {
            "avg": round(max(0.0, nh3), 1),
            "min": round(max(0.0, nh3 - 1.0), 1),
            "max": round(max(0.0, nh3 + 1.0), 1),
            "max_ts": 59 if velocity else 0,
        },
        "humidity": {"avg": round(min(100.0, max(0.0, humidity)), 1)},
    }


def _jitter(window_index: int, saloon_index: int, pack_index: int, scale: float) -> float:
    """Deterministic pseudo-noise so consecutive windows differ visibly."""
    rnd = random.Random(hash((window_index // 4, saloon_index, pack_index)))
    return (rnd.random() * 2 - 1) * scale


def _in_los_window(saloon_index: int, pack_index: int, window_index: int, scenario: dict) -> bool:
    """True when this pack's device is scripted SILENT (LOS outage) this window."""
    pack_suffix = "AB"[pack_index]
    for (start, end, suffix) in scenario.get("los_windows", {}).get(saloon_index, []):
        if suffix == pack_suffix and start <= window_index <= end:
            return True
    # Farm-wide correlation outage silences EVERY pack.
    for (start, end) in scenario.get("correlation_windows", []):
        if start <= window_index <= end:
            return True
    return False


def _in_replay_window(saloon_index: int, pack_index: int, window_index: int, scenario: dict) -> bool:
    """True when this pack is inside a scripted replay (buffered) window."""
    pack_suffix = "AB"[pack_index]
    for (start, end, suffix) in scenario.get("replay_windows", {}).get(saloon_index, []):
        if suffix == pack_suffix and start <= window_index <= end:
            return True
    return False


def replay_device_ts(device_ts: datetime, offset_minutes: int = 60) -> datetime:
    """Buffered readings carry a device_ts stepped BACK by offset_minutes so
    the historical drain replays a genuinely OLD window (never paged)."""
    return device_ts - timedelta(minutes=offset_minutes)


def scenario_factor(window_index: int, scenario: dict, saloon_index: int) -> float:
    """1.0 during a scripted NH3 breach window for the saloon, else 0.0.

    Lets callers label chart points / alert rows honestly ("breach window").
    """
    for (start, end, _suffix, _drift) in scenario.get("slot_breach_windows", {}).get(saloon_index, []):
        if start <= window_index <= end:
            return 1.0
    return 0.0


def build_payload(
    hardware_id: str,
    slot_id: str,
    pack_id: str,
    device_ts: datetime,
    metrics: dict,
    buffered: bool = False,
    clock_drift: bool = False,
) -> dict:
    """One schema-2.0 payload (Firmware Spec §payload) — one pack per payload
    so each fake device owns its own packet, mirroring a real pack's MQTT."""
    return {
        "gateway_hardware_id": hardware_id,
        "gateway_slot_id": slot_id,
        "schema_version": "2.0",
        "device_ts": device_ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "is_buffered": buffered,
        "clock_drift": clock_drift,
        "readings": [{"pack_id": pack_id, "metrics": metrics}],
    }


def window_stepper(start_ts: datetime, interval_minutes: int = 1, start_index: int = 0):
    """Yield (device_ts, window_index) advancing `interval_minutes` per step,
    starting at `start_ts` truncated to the interval + start_index steps."""
    start = start_ts.replace(second=0, microsecond=0)
    idx = start_index
    while True:
        yield start + timedelta(minutes=interval_minutes * idx), idx
        idx += 1


# --------------------------------------------------------------------------
# MQTT publishing (per-pack fake device) — paho is lazy-imported so tests can
# import this module without the dependency.
# --------------------------------------------------------------------------


def _connect_args(args, farm_id=None):
    """Per-farm MQTT identity so EMQX ACLs can pin each pack to ITS OWN farm:
    the username is {prefix}-farm{farm_id} (e.g. demo-sim-farm6), which the
    broker's rules allow to publish only that farm's topics. farm_id None
    (a manifest without farms) falls back to the bare prefix."""
    prefix = getattr(args, "mqtt_username_prefix", "demo-sim") or None
    username = None
    if prefix is not None:
        username = f"{prefix}-farm{farm_id}" if farm_id is not None else prefix
    return {
        "host": getattr(args, "host", "localhost"),
        "port": getattr(args, "port", 1883),
        "username": username,
        "password": getattr(args, "mqtt_password", None) or None,
    }


def _topic(org_id: str, farm_id, slot_id: str) -> str:
    return f"{org_id}/farms/{farm_id}/gateways/{slot_id}/telemetry"


def status_topic(org_id: str, farm_id, slot_id: str) -> str:
    """Pack-liveness status topic — mirrors the telemetry topic exactly (same
    org/farm/gateway segments) with a /status leaf: farm/{farm}/gateways/{gw}/
    status. Retained online on connect, offline via the Last Will on death.
    فارسی: تاپیک وضعیت پک — همان مسیر تله‌متری با برگِ /status."""
    return f"{org_id}/farms/{farm_id}/gateways/{slot_id}/status"


def pack_status_payload(status: str, hardware_id: str, slot_id: str, pack_id: str) -> dict:
    """One retained pack-liveness payload — schema-2.0 shape so the subscriber
    parses it with the same conventions as telemetry. `status` is 'online'
    (published on connect) or 'offline' (the Last Will on unclean death).
    فارسی: بدنهٔ وضعیت پک — شکل اسکیما-۲ برای مشترک."""
    if status not in ("online", "offline"):
        raise ValueError(f"pack status must be 'online' or 'offline', got {status!r}")
    return {
        "status": status,
        "schema_version": "2.0",
        "gateway_hardware_id": hardware_id,
        "gateway_slot_id": slot_id,
        "pack_id": pack_id,
        "ts": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _manifest_slots(manifest: dict, default_farm_id=None):
    """(saloon_index, pack_index, slot) triples, each slot enriched with its
    OWN saloon's farm_id. Honesty fix: the manifest's top-level farm_id is
    just the seed's first farm, and every pack used to be published under it
    — a pack in Morvarid was telling the broker it lived in Baharestan. The
    status topic (and telemetry topic) must say where the saloon actually is.
    فارسی: هر پک زیر مزرعهٔ واقعی سالن خودش منتشر شود."""
    if default_farm_id is None:
        default_farm_id = manifest.get("farm_id", 1)
    out = []
    for saloon_index, saloon in enumerate(manifest["saloons"]):
        saloon_farm = saloon.get("farm_id", default_farm_id)
        for pack_index, slot in enumerate(saloon["slots"]):
            enriched = dict(slot)
            enriched["saloon_index"] = saloon_index
            enriched["farm_id"] = saloon_farm
            out.append((saloon_index, pack_index, enriched))
    return out


def publish_loop(
    args,
    stop: threading.Event,
    org_id: str,
    farm_id,
    slot: dict,
    pack_index: int,
    scenario: dict,
    interval_sec: float,
    on_publish=None,
) -> None:
    """One fake device: publish a payload every `interval_sec` for its own slot.

    window_index advances ONE 60s device window per wall-clock tick, so the
    dedup guard never sees a repeated device_ts and charts move at a demo pace.
    """
    import paho.mqtt.client as mqtt

    hardware_id = slot["hardware_id"]
    slot_id = slot["slot_id"]
    pack_id = slot["pack"]
    # Each pack publishes under the farm its OWN saloon belongs to (honesty
    # fix — the manifest's top-level farm_id used to be applied to every
    # pack, so Morvarid packs claimed to live in Baharestan).
    farm_id = slot.get("farm_id", farm_id)
    topic = _topic(org_id, farm_id, slot_id)
    lwt_topic = status_topic(org_id, farm_id, slot_id)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"farm-sim-{pack_id}-{slot_id}-{hash((org_id, slot_id)) & 0xFFFF}",
    )
    conn = _connect_args(args, farm_id)
    if conn["username"]:
        client.username_pw_set(conn["username"], conn["password"])

    # Last Will: each pack thread owns its OWN mqtt.Client, so a dead pack
    # IS a dead connection — the broker publishes this retained offline state
    # onto the pack's status topic the moment that connection dies uncleanly
    # (process/thread killed). The board then sees the death immediately,
    # without waiting for the sensor-status TTL to lapse. The offline publish
    # below suppresses the will on clean shutdown (DISCONNECT), and we write
    # the offline state ourselves instead so the retained word stays honest.
    client.will_set(
        lwt_topic,
        json.dumps(pack_status_payload("offline", hardware_id, slot_id, pack_id)).encode(),
        qos=1,
        retain=True,
    )

    # Broker drop-in visibility: EMQX restarts are invisible to the publish
    # loop, so log every connect/disconnect into the demo sim log (observed
    # need: a stalled demo traced to a silently reconnected broker).
    def _on_conn(_c, _u, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            print(f"[sim:{pack_id}] CONNACK failure: {reason_code}", flush=True)
        else:
            print(f"[sim:{pack_id}] connected to broker (session_present={flags.session_present})", flush=True)

    def _on_disc(_c, _u, disconnect_flags, reason_code, properties=None):
        print(f"[sim:{pack_id}] disconnected from broker: {reason_code}", flush=True)

    client.on_connect = _on_conn
    client.on_disconnect = _on_disc

    # Boot with retry/backoff: a broker that is down at start must not kill
    # the pack thread silently (loop_start() then rides paho's auto-reconnect
    # for any later drop).
    attempt = 0
    while not stop.is_set():
        try:
            client.connect(conn["host"], conn["port"], keepalive=60)
            break
        except OSError as exc:
            attempt += 1
            delay = min(2 ** attempt, 15)
            print(f"[sim:{pack_id}] broker unreachable ({exc}); retry in {delay}s", flush=True)
            time.sleep(delay)
    if stop.is_set():
        return
    client.loop_start()

    # Retained ONLINE state: while the pack lives, the last word on its
    # status topic is 'online' (overwrites any stale retained 'offline' from
    # a previous run); the LWT flips it back on death. LOS windows don't
    # touch this — a SILENT pack is not a DEAD pack, so its status stays
    # 'online' throughout a scripted outage.
    client.publish(
        lwt_topic,
        json.dumps(pack_status_payload("online", hardware_id, slot_id, pack_id)).encode(),
        qos=1,
        retain=True,
    )

    start_index = getattr(args, "start_window", 0)
    stepper = window_stepper(_utcnow(), start_index=start_index)
    try:
        while not stop.is_set():
            device_ts, window_index = next(stepper)

            # LOS / correlation windows: the device is SILENT — nothing is
            # published, so its sensor:status key expires and the watchdog
            # sees a real outage (then auto-resolves when publishing resumes).
            if _in_los_window(slot.get("saloon_index", 0), pack_index, window_index, scenario):
                time.sleep(interval_sec)
                continue

            metrics = value_at(
                slot.get("saloon_index", 0),
                pack_index,
                window_index,
                scenario,
            )

            # Replay windows: publish BUFFERED readings with device_ts stepped
            # back an hour — the historical lane drains the backlog, hits
            # replay:pending 0, and the retroactive sweep writes REPLAY_ANALYSIS
            # rows (historical review, never pushed).
            buffered = _in_replay_window(slot.get("saloon_index", 0), pack_index, window_index, scenario)
            if buffered:
                device_ts = replay_device_ts(device_ts)

            payload = build_payload(
                hardware_id, slot_id, pack_id, device_ts, metrics,
                buffered=buffered,
            )
            raw = json.dumps(payload).encode()
            if getattr(args, "dry_run", False):
                print(f"[dry-run] {pack_id} {slot_id} -> {topic}\n{json.dumps(payload, indent=2)}")
            else:
                client.publish(topic, raw, qos=1)
                if on_publish:
                    on_publish(slot_id, window_index)
            time.sleep(interval_sec)
    finally:
        # Clean shutdown: the retained 'online' must not outlive the pack, so
        # write the offline state ourselves, let the loop flush it, then
        # disconnect WITHOUT triggering the will (a clean DISCONNECT
        # suppresses it — the retained state is already honest).
        try:
            client.publish(
                lwt_topic,
                json.dumps(pack_status_payload("offline", hardware_id, slot_id, pack_id)).encode(),
                qos=1,
                retain=True,
            ).wait_for_publish(timeout=2.0)
        except Exception:
            pass  # broker gone — the LWT itself covers the death
        client.loop_stop()
        client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simulate_farm",
        description=(
            "Live-demo farm simulator — fake gateways/devices publishing real "
            "schema-2.0 payloads over the REAL MQTT path into the dashboard."
        ),
    )
    parser.add_argument(
        "--manifest", required=True,
        help="Path to the topology manifest JSON (farm/saloon ids the sim publishes for)",
    )
    parser.add_argument(
        "--interval", type=float, default=5.0,
        help="Wall-clock seconds between publishes per pack (default 5 — charts move fast)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print payloads instead of publishing (never touches the broker)",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument(
        "--mqtt-username-prefix", default="demo-sim",
        help="MQTT username prefix; each pack authenticates as {prefix}-farm{farm} "
             "so EMQX ACLs pin it to its own farm (default demo-sim)",
    )
    parser.add_argument("--mqtt-password", default=None)
    parser.add_argument("--scenario", default="demo",
                        choices=["demo", "calm"],
                        help="demo = scripted breach/velocity scenarios; calm = no breaches")
    parser.add_argument(
        "--start-window", type=int, default=0,
        help="Skip this many 60s device windows before publishing (0 = from now). "
             "Use e.g. 25 after a pause so the NEXT breach window is close.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with open(args.manifest, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except OSError as exc:
        print(f"cannot read manifest {args.manifest!r}: {exc}")
        return 1

    org_id = manifest["org_id"]
    farm_id = manifest.get("farm_id", 1)
    saloons = manifest["saloons"]
    slots = _manifest_slots(manifest, default_farm_id=farm_id)
    print(f"simulating {len(slots)} fake devices across {len(saloons)} saloons "
          f"(org {org_id}; each pack publishes under ITS OWN saloon's farm)")

    scenario = {
        "slot_breach_windows": SLOT_BREACH_WINDOWS if args.scenario == "demo" else {},
        "velocity_windows": VELOCITY_WINDOWS if args.scenario == "demo" else {},
        "temp_breach_windows": TEMP_BREACH_WINDOWS if args.scenario == "demo" else {},
        "los_windows": LOS_WINDOWS if args.scenario == "demo" else {},
        "correlation_windows": CORRELATION_WINDOWS if args.scenario == "demo" else [],
        "replay_windows": REPLAY_WINDOWS if args.scenario == "demo" else {},
        "thi_windows": THI_WINDOWS if args.scenario == "demo" else {},
    }

    stop = threading.Event()
    threads = []
    for saloon_index, pack_index, slot in slots:
        t = threading.Thread(
            target=publish_loop,
            args=(args, stop, org_id, farm_id, slot, pack_index, scenario, args.interval),
            daemon=True,
        )
        t.start()
        threads.append(t)

    print("publishing (Ctrl-C to stop)...")
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping...")
        stop.set()
        for t in threads:
            t.join(timeout=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
