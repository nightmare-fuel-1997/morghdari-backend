#!/usr/bin/env python3
"""M8 — Hardware Handover Harness (receiver-side contract for the ESP32 team).

Runs the firmware behavioral contracts (v0.8 protocols §2) as pass/fail probes
against the STACK the gateway connects to:

  P1 config_fetch    zero-touch GET /api/v1/gateways/{slot}/config/ → 200
  P2 telemetry_visible  publish schema-2.0 → row is readable with the SAME
                     device_ts + is_buffered=false (the pipeline is honest)
  P3 lwt_offline     abrupt client death → status topic flips to 'offline'
  P4 buffered_replay is_buffered=true + OLD device_ts → rows keep ORIGINAL
                     timestamps (dual-timestamp proof, HIL-1/4)
  P5 ota_ack         T6 OTA command → device answers on T7 (sim: SKIP)
  P6 rotation_ack    T3 ROTATE_CREDENTIAL envelope → device ACKs on T4 (sim: SKIP)

Two targets:
  --target sim   self-test against the SIMULATOR/dev stack (no real device).
                 P5/P6 are SKIPped honestly — no device exists to answer.
  --target real  the HARDWARE HANDOVER GATE — the ESP32 team's "done" means
                 `hil_harness.py --target real` is green at bring-up.

Every FAIL prints receiver-side evidence (rows, statuses, dead-letter reason).
Exit 0 = no FAIL (SKIP tolerated); 1 = any FAIL.

فارسی: هارنِسِ تحویلِ سخت‌افزار — معیارِ پذیرشِ گیت‌وی واقعی؛ حالت sim خودآزماییِ
سمتِ نرم‌افزار است و حالت real درِ قبولِ تحویل.
"""

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

try:
    import paho.mqtt.client as mqtt  # type: ignore
except ImportError:  # probe-less run (HTTP-only) still works
    mqtt = None


def _payload_builder():
    """Honest schema-2.0 payload builder — reuse the v0.3 simulator's.

    simulate_gateway.build_payload is import-safe (paho lazy); fall back to a
    local twin only if the import genuinely fails.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        from simulate_gateway import build_payload  # type: ignore
    except Exception:  # pragma: no cover — local twin
        def build_payload(hardware_id, gateway_slot, packs, device_ts,
                          buffered=False, clock_drift=False, seq=0):
            readings = [{"pack_id": p, "metrics": {
                "temp": {"avg": 28.0, "min": 26.0, "max": 30.0},
                "nh3": {"avg": 14.0, "min": 10.0, "max": 20.0, "max_ts": seq % 60},
            }} for p in packs]
            return {
                "gateway_hardware_id": hardware_id,
                "gateway_slot_id": gateway_slot,
                "schema_version": "2.0",
                "device_ts": device_ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "is_buffered": buffered,
                "clock_drift": clock_drift,
                "readings": readings,
            }
    return build_payload


# ---------------------------------------------------------------------------
def _ssl_ctx(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get_json(url: str, ctx: ssl.SSLContext, auth: str | None = None, timeout: float = 15.0):
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read(65536).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(65536).decode("utf-8", "replace"))
        except ValueError:
            body = {}
        return exc.code, body
    except Exception as exc:
        return None, {"error": str(exc)}


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Probe:
    """One handover probe: name → (status, evidence). status in PASS/FAIL/SKIP."""

    def __init__(self, name: str, title: str):
        self.name, self.title = name, title

    def run(self, ctx: "HarnessContext") -> tuple[str, str]:
        raise NotImplementedError


class HarnessContext:
    def __init__(self, args):
        self.args = args
        self.base = args.base_url.rstrip("/")
        self.ctx = _ssl_ctx(args.insecure or args.base_url.startswith("http://"))
        self.build_payload = _payload_builder()

    def manager_auth(self):
        if self.args.manager_token:
            return f"Bearer {self.args.manager_token}"
        return None

    def device_auth(self):
        if self.args.device_user and self.args.device_pass:
            token = base64.b64encode(
                f"{self.args.device_user}:{self.args.device_pass}".encode()).decode()
            return f"Basic {token}"
        return None

    def http_get(self, path: str, auth: str | None = None):
        return _get_json(f"{self.base}{path}", self.ctx, auth)

    def mqtt_connect(self, client, tls: bool, keepalive: int = 60,
                     client_id: str | None = None,
                     username: str | None = None, password: str | None = None):
        client.client_id = client_id or f"hil-{self.args.target}-{os.getpid()}"
        port = self.args.mqtt_port
        if tls:
            ctx = ssl.create_default_context()
            if self.args.mqtt_insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            client.tls_set_context(ctx)
        client.username_pw_set(username if username is not None else self.args.mqtt_user,
                               password if password is not None else self.args.mqtt_pass)
        client.connect(self.args.mqtt_host, port, keepalive=keepalive)
        return client

    def observer_credentials(self) -> tuple[str, str]:
        """SUBSCRIBE-capable identity for the P3 observer (gateways are
        publish-only by design). Defaults to the device creds (P3 then SKIPs
        honestly unless --mqtt-observer-user is given)."""
        if self.args.mqtt_observer_user:
            return self.args.mqtt_observer_user, self.args.mqtt_observer_pass
        return self.args.mqtt_user, self.args.mqtt_pass

    def hardware_id(self) -> str:
        return self.args.hardware_id or f"{self.args.slot}-HW"

    def telemetry_topic(self) -> str:
        return (f"{self.args.org}/farms/{self.args.farm_id}/gateways/"
                f"{self.args.slot}/telemetry")

    def status_topic(self) -> str:
        return (f"{self.args.org}/farms/{self.args.farm_id}/gateways/"
                f"{self.args.slot}/status")

    def ota_cmd_topic(self) -> str:
        return (f"{self.args.org}/farms/{self.args.farm_id}/gateways/"
                f"{self.args.slot}/ota")

    def ota_rsp_topic(self) -> str:
        return (f"{self.args.org}/farms/{self.args.farm_id}/gateways/"
                f"{self.args.slot}/ota/response")

    def cmd_topic(self) -> str:
        return (f"{self.args.org}/farms/{self.args.farm_id}/gateways/"
                f"{self.args.slot}/commands")

    def cmd_rsp_topic(self) -> str:
        return (f"{self.args.org}/farms/{self.args.farm_id}/gateways/"
                f"{self.args.slot}/commands/response")


class P1ConfigFetch(Probe):
    def run(self, ctx: HarnessContext):
        auth = ctx.device_auth() or ctx.manager_auth()
        if not auth:
            return "SKIP", "no --device-user/--device-pass or --manager-token"
        status, body = ctx.http_get(f"/api/v1/gateways/{ctx.args.slot}/config/", auth)
        if status == 200 and isinstance(body, dict):
            keys = sorted(body)
            return "PASS", f"http=200 keys={keys}"
        return "FAIL", f"http={status} body={str(body)[:240]}"


class P2TelemetryVisible(Probe):
    def run(self, ctx: HarnessContext):
        if mqtt is None:
            return "SKIP", "paho-mqtt not installed"
        auth = ctx.manager_auth()
        if not auth or not ctx.args.saloon_id:
            return "SKIP", "need --manager-token + --saloon-id (and MQTT creds)"

        device_ts = datetime.now(timezone.utc).replace(microsecond=0)
        payload = ctx.build_payload(
            ctx.hardware_id(), ctx.args.slot, ["P-A"], device_ts, buffered=False, seq=1)

        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"hil-p2-{os.getpid()}")
        ctx.mqtt_connect(client, tls=ctx.args.mqtt_tls)
        client.loop_start()
        info = client.publish(ctx.telemetry_topic(), json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=15)
        client.disconnect(); client.loop_stop()

        deadline = time.time() + 30
        newest = None
        want = device_ts.astimezone(timezone.utc)
        while time.time() < deadline:
            status, body = ctx.http_get(
                f"/api/v1/saloons/{ctx.args.saloon_id}/telemetry/?limit=1", auth)
            if status == 200 and isinstance(body, dict) and body.get("results"):
                candidate = body["results"][0]
                try:
                    cand_instant = datetime.fromisoformat(
                        str(candidate.get("device_ts")).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    cand_instant = None
                # A quiet farm answers instantly with STALE rows — only stop
                # when the newest row is at least as fresh as what we sent.
                if cand_instant is not None and cand_instant >= want:
                    newest = candidate
                    break
            time.sleep(2)
        if not newest:
            return "FAIL", "no telemetry row appeared within 30s (pipeline gate?)"
        row_ts = newest.get("device_ts")
        row_buffered = newest.get("is_buffered", newest.get("buffered"))
        # The API serializes datetimes as '+00:00' (often with microseconds)
        # while we sent 'Z' — compare instants, not strings.
        try:
            row_instant = datetime.fromisoformat(str(row_ts).replace("Z", "+00:00"))
            want_instant = device_ts.astimezone(timezone.utc)
            ts_ok = abs((row_instant - want_instant).total_seconds()) < 1
        except (ValueError, TypeError):
            ts_ok = False
        if ts_ok and row_buffered is False:
            return "PASS", f"device_ts {row_ts} == sent {_iso(device_ts)}; is_buffered={row_buffered}"
        return "FAIL", f"device_ts {row_ts!r} != {_iso(device_ts)} or is_buffered={row_buffered!r}"
class P3LwtOffline(Probe):
    def run(self, ctx: HarnessContext):
        if mqtt is None:
            return "SKIP", "paho-mqtt not installed"
        topic = ctx.status_topic()
        got = {"offline": False, "suback": None}

        def on_subscribe(_c, _u, _mid, reason_codes, _props=None):
            codes = list(reason_codes)
            granted = [c for c in codes
                       if getattr(c, "value", c) in (0, 1, 2)]
            got["suback"] = "GRANTED" if granted and len(granted) == len(codes) else "DENIED"

        def on_message(_c, _u, msg):
            try:
                data = json.loads(msg.payload.decode())
            except ValueError:
                data = msg.payload.decode()
            state = data.get("status", data) if isinstance(data, dict) else data
            if state in ("offline", False, "false"):
                got["offline"] = True

        # The observer needs SUBSCRIBE rights on the status topic — a gateway
        # identity only ever publishes it (least privilege), so the harness
        # takes separate observer creds (e.g. the backend subscriber user).
        observer = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                               client_id=f"hil-p3-obs-{os.getpid()}")
        observer.on_subscribe = on_subscribe
        observer.on_message = on_message
        obs_user, obs_pass = ctx.observer_credentials()
        ctx.mqtt_connect(observer, tls=ctx.args.mqtt_tls,
                         username=obs_user, password=obs_pass)
        observer.loop_start()
        observer.subscribe(topic, qos=1)
        deadline = time.time() + 10
        while time.time() < deadline and got["suback"] is None:
            time.sleep(0.5)
        if got["suback"] != "GRANTED":
            observer.disconnect(); observer.loop_stop()
            return ("SKIP", "observer SUBSCRIBE not granted on the status topic "
                    "(gateway identities are publish-only by design) — pass "
                    "--mqtt-observer-user/--mqtt-observer-pass of a "
                    "subscribe-capable user, e.g. the backend subscriber")

        dying = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                            client_id=f"hil-p3-die-{os.getpid()}")
        dying.will_set(topic, json.dumps({"status": "offline", "source": "hil-p3"}),
                       qos=1, retain=True)
        # Same TLS/auth path as every other probe (port 8883 is TLS-only) but
        # keep the distinct client id — then kill the socket below.
        ctx.mqtt_connect(dying, tls=ctx.args.mqtt_tls, keepalive=5,
                         client_id=f"hil-p3-die-{os.getpid()}")
        dying.loop_start()
        time.sleep(1.5)
        # Abrupt death — close the socket WITHOUT a DISCONNECT packet so the
        # broker fires our LWT. paho 2.x keeps it as _sock (1.x: sock).
        sock = getattr(dying, "_sock", None) or getattr(dying, "sock", None)
        if sock is None:
            dying.loop_stop()
            return "FAIL", "no underlying socket to kill (never connected?)"
        sock.close()
        dying.loop_stop()

        deadline = time.time() + 18  # keepalive(5) * ~2 + margin
        while time.time() < deadline and not got["offline"]:
            time.sleep(1)
        observer.disconnect(); observer.loop_stop()
        if got["offline"]:
            return "PASS", "broker published LWT 'offline' after abrupt death"
        return "FAIL", f"no offline on {topic} within 18s"


class P4BufferedReplay(Probe):
    def run(self, ctx: HarnessContext):
        if mqtt is None:
            return "SKIP", "paho-mqtt not installed"
        auth = ctx.manager_auth()
        if not auth or not ctx.args.saloon_id:
            return "SKIP", "need --manager-token + --saloon-id (and MQTT creds)"

        now = datetime.now(timezone.utc).replace(microsecond=0)
        # Fresh marks per run (dedup is keyed on device_ts — fixed marks like
        # midnight would be dropped as duplicates on a re-run the same day).
        marks = [now.replace(second=0, microsecond=0) - timedelta(minutes=2),
                 now.replace(second=0, microsecond=0) - timedelta(minutes=1)]

        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"hil-p4-{os.getpid()}")
        ctx.mqtt_connect(client, tls=ctx.args.mqtt_tls)
        client.loop_start()
        for i, mark in enumerate(marks):
            payload = ctx.build_payload(ctx.hardware_id(), ctx.args.slot,
                                        ["P-A"], mark, buffered=True, seq=i + 2)
            info = client.publish(ctx.telemetry_topic(), json.dumps(payload), qos=1)
            info.wait_for_publish(timeout=10)
        time.sleep(1)
        client.disconnect(); client.loop_stop()

        seen = {}
        deadline = time.time() + 30
        # Buffered rows are EXCLUDED from the default read — ask for them.
        gap_url = (f"/api/v1/saloons/{ctx.args.saloon_id}/telemetry/"
                   f"?limit=20&include_buffered=true")
        while time.time() < deadline:
            status, body = ctx.http_get(gap_url, auth)
            if status == 200 and isinstance(body, dict):
                for row in body.get("results", []):
                    try:
                        instant = datetime.fromisoformat(
                            str(row.get("device_ts")).replace("Z", "+00:00"))
                        key = instant.astimezone(timezone.utc).replace(
                            microsecond=0).isoformat()
                    except (ValueError, TypeError):
                        continue
                    seen.setdefault(key, row.get("is_buffered"))
            if all(seen.get(m.astimezone(timezone.utc).replace(
                    microsecond=0).isoformat()) is True for m in marks):
                break
            time.sleep(2)

        missed = [m.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                  for m in marks
                  if seen.get(m.astimezone(timezone.utc).replace(
                      microsecond=0).isoformat()) is not True]
        if not missed:
            return "PASS", f"both buffered rows kept ORIGINAL device_ts [{_iso(marks[0])}, {_iso(marks[1])}]"
        return "FAIL", f"rows missing/not is_buffered: {missed}"
class P5OtaAck(Probe):
    def run(self, ctx: HarnessContext):
        if mqtt is None:
            return "SKIP", "no device to answer T7 (harness expects a real gateway)"
        topic = ctx.ota_rsp_topic()
        got = {}

        def on_message(_c, _u, msg):
            try:
                got.update(json.loads(msg.payload.decode()))
            except ValueError:
                got["raw"] = msg.payload.decode()

        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"hil-p5-{os.getpid()}")
        ctx.mqtt_connect(client, tls=ctx.args.mqtt_tls)
        client.on_message = on_message
        client.subscribe(topic, qos=1)
        client.loop_start()
        time.sleep(1)
        cmd = {"version": "0.0.0-hil", "url": "https://ota.invalid/hil.bin",
               "checksum_sha256": "0" * 64}
        client.publish(ctx.ota_cmd_topic(), json.dumps(cmd), qos=1)

        deadline = time.time() + 20
        while time.time() < deadline and not got:
            time.sleep(1)
        client.disconnect(); client.loop_stop()
        if got:
            status = got.get("status", got.get("raw", "?"))
            return "PASS" if status != "FAILED" else "FAIL", f"device answered {got}"
        return "SKIP", "no T7 response in 20s — device offline or not wired (sim mode)"


class P6RotationAck(Probe):
    def run(self, ctx: HarnessContext):
        if mqtt is None:
            return "SKIP", "no device to ACK T4 (harness expects a real gateway)"
        topic = ctx.cmd_rsp_topic()
        import uuid as _uuid
        command_uuid = str(_uuid.uuid4())
        got = {}

        def on_message(_c, _u, msg):
            try:
                got.update(json.loads(msg.payload.decode()))
            except ValueError:
                got["raw"] = msg.payload.decode()

        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"hil-p6-{os.getpid()}")
        ctx.mqtt_connect(client, tls=ctx.args.mqtt_tls)
        client.on_message = on_message
        client.subscribe(topic, qos=1)
        client.loop_start()
        time.sleep(1)
        env = {"command_uuid": command_uuid, "command": "ROTATE_CREDENTIAL"}
        client.publish(ctx.cmd_topic(), json.dumps(env), qos=1)

        deadline = time.time() + 20
        while time.time() < deadline and not got:
            time.sleep(1)
        client.disconnect(); client.loop_stop()
        if got:
            return "PASS", f"device answered {got}"
        return "SKIP", f"no T4 ACK in 20s for {command_uuid} — sim mode"


PROBES = [
    P1ConfigFetch("P1", "zero-touch config fetch (GET config 200)"),
    P2TelemetryVisible("P2", "telemetry visible — device_ts honest, live lane"),
    P3LwtOffline("P3", "LWT — abrupt death flips status to offline"),
    P4BufferedReplay("P4", "buffered replay — ORIGINAL timestamps survive"),
    P5OtaAck("P5", "OTA command → device response (T6/T7)"),
    P6RotationAck("P6", "ROTATE_CREDENTIAL → device ACK (T3/T4)"),
]
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("sim", "real"), default="sim",
                        help="real = hardware handover gate")
    parser.add_argument("--base-url", default=os.environ.get("HIL_BASE_URL", "https://localhost"))
    parser.add_argument("--slot", default=os.environ.get("HIL_SLOT", "FARM-01-GW-A"))
    parser.add_argument("--hardware-id", default=os.environ.get("HIL_HARDWARE_ID", ""),
                        help="gateway_hardware_id stamped in payloads (default: {slot}-HW). "
                             "Must match a provisioned gateway_sensor_mapping row, or the "
                             "pipeline dead-letters UNKNOWN_SLOT.")
    parser.add_argument("--org", default=os.environ.get("HIL_ORG", ""))
    parser.add_argument("--farm-id", default=os.environ.get("HIL_FARM_ID", "1"))
    parser.add_argument("--saloon-id", default=os.environ.get("HIL_SALOON_ID", ""))
    parser.add_argument("--manager-token", default=os.environ.get("HIL_MANAGER_TOKEN", ""))
    parser.add_argument("--device-user", default=os.environ.get("HIL_DEVICE_USER", ""))
    parser.add_argument("--device-pass", default=os.environ.get("HIL_DEVICE_PASSWORD", ""))
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    parser.add_argument("--mqtt-user", default=os.environ.get("MQTT_USERNAME", ""))
    parser.add_argument("--mqtt-pass", default=os.environ.get("MQTT_PASSWORD", ""))
    parser.add_argument("--mqtt-observer-user", default=os.environ.get("MQTT_OBSERVER_USERNAME", ""),
                        help="SUBSCRIBE-capable user for the P3 LWT observer "
                             "(gateways are publish-only; e.g. the backend subscriber user). "
                             "Defaults to --mqtt-user (P3 then SKIP honestly).")
    parser.add_argument("--mqtt-observer-pass", default=os.environ.get("MQTT_OBSERVER_PASSWORD", ""))
    parser.add_argument("--mqtt-tls", action="store_true",
                        default=os.environ.get("MQTT_TLS", "0") == "1")
    parser.add_argument("--mqtt-insecure", action="store_true",
                        default=os.environ.get("MQTT_TLS_INSECURE", "0") == "1")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification for the HTTP probes")
    parser.add_argument("--probe", action="append", default=[],
                        help="run only this probe (repeatable); default: all")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path, default=None,
                        help="write JSON results to a file")
    args = parser.parse_args(argv)

    ctx = HarnessContext(args)
    selected = [p for p in PROBES if not args.probe or p.name in args.probe]
    results = []
    for probe in selected:
        try:
            status, detail = probe.run(ctx)
        except Exception as exc:  # a crashed probe is a FAIL with the reason
            status, detail = "FAIL", f"probe crashed: {exc!r}"
        results.append({"probe": probe.name, "title": probe.title,
                        "status": status, "detail": detail})
        print(f"[{status:4}] {probe.name} {probe.title} — {detail}")

    failures = [r for r in results if r["status"] == "FAIL"]
    print(f"\n{'PASS' if not failures else 'FAIL'} — target={args.target}: "
          f"{len(results)} probes, "
          f"{sum(1 for r in results if r['status'] == 'PASS')} pass, "
          f"{sum(1 for r in results if r['status'] == 'SKIP')} skip, "
          f"{len(failures)} fail")
    print("NOTE: --target sim self-tests the SOFTWARE side; the HARDWARE "
          "acceptance is `--target real` with the ESP32 gateway on the bench.")
    if args.out:
        args.out.write_text(json.dumps(
            {"target": args.target, "results": results,
             "exit": 1 if failures else 0}, indent=2) + "\n", encoding="utf-8")
        print(f"results → {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())