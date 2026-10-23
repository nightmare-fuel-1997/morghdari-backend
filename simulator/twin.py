"""twin.py — a digital-twin gateway: one simulated physical device.

WHAT THIS IS
------------
A realistic counterpart of the hardware described in
`docs/hardware-firmware-requirements-v1.md`. It speaks the same protocols,
authenticates the same way, registers the same way, sends the same payload
shapes at the same intervals, goes through the same states, fails the same
ways, and reconnects the same way. Nothing here is a shortcut around the
backend: every message it publishes is one a real gateway could publish, and
the backend cannot tell the difference.

INDEPENDENCE RULE (do not remove): this file MUST NOT import anything from
`apps/` or `morghdari/`. It implements the contract from scratch, using only
the topic strings and payload shapes written in the requirements document.
The moment it imports the backend's own `topics.py`, a bug in our shared
grammar stops being caught by the run — the test becomes a tautology.

WHAT IT MODELS
--------------
  * boot → connect (TLS, MQTT 3.1.1, username = unit_id) → subscribe
  * retained `desired` arrives → validate → monotonic-guard → apply → report
    `APPLIED` (the config-apply uplink that makes PROVISIONED reachable)
  * after binding: tenant telemetry (schema 2.0, per-pack readings), status
    with a Last Will, and the tenant command lane
  * before binding: the device lane for commands and their responses
  * reconnection with backoff, and `applied_version` surviving a reboot
  * faults: dropped connectivity, delayed sends, corrupt readings, restarts

KNOWN LIMITATIONS (documented rather than approximated)
-----------------------------------------------------
  * Sensor physics is a diurnal model, not a real shed. Values are plausible
    and in range; they are not a thermal simulation.
  * There is no TLS client certificate. The fleet authenticates with
    username/password (Phase 1 posture), so neither does the twin.
  * `ROTATE_CREDENTIAL` defers the reconnect by design — see `_cmd_rotate`.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger("sim.twin")

# Device-lane control protocol (§3.3/§3.6 payloads).
SCHEMA_VERSION = "1.0"
# What the ingest pipeline accepts (Firmware Spec §3.8 / pipeline Gate 3).
TELEMETRY_SCHEMA = "2.0"

# Fleet pin — owner decision 2026-10-18 (docs Tracker §4t).
MQTT_PROTOCOL = 4  # 3.1.1


# ---- the contract, transcribed by hand from the requirements document -------

def desired_topic(unit_id: str) -> str:
    return f"devices/{unit_id}/desired"


def command_topic(unit_id: str) -> str:
    return f"devices/{unit_id}/command"


def reported_topic(unit_id: str) -> str:
    return f"devices/{unit_id}/reported"


def command_response_topic(unit_id: str) -> str:
    """`devices/{unit_id}/command/response` — the pre-bind response lane.

    Deliberately NOT the tenant `commands/response` lane: an UNBOUND device has
    no org, so it has no tenant topic to answer on. The server accepts both and
    correlates by `command_uuid`.
    """
    return f"devices/{unit_id}/command/response"


def tenant_topic(org_id: str, farm_id, slot_id: str, suffix: str) -> str:
    return f"{org_id}/farms/{farm_id}/gateways/{slot_id}/{suffix}"


# ---- realistic sensor physics ------------------------------------------------

METRIC_BASE = {
    "temp": 24.5, "humidity": 62.0, "nh3": 8.0, "co": 4.0,
    "lux": 320.0, "water_l": 1.2, "power_mains": 220.0, "power_gen": 0.0,
}
METRIC_AMPLITUDE = {
    "temp": 1.5, "humidity": 5.0, "nh3": 2.0, "co": 1.0,
    "lux": 40.0, "water_l": 0.4, "power_mains": 3.0, "power_gen": 0.0,
}
# A reading outside these bounds is physically implausible for a poultry shed
# and is what a real device would flag as a sensor fault.
METRIC_RANGE = {
    "temp": (10.0, 45.0), "humidity": (20.0, 95.0), "nh3": (0.0, 60.0),
    "co": (0.0, 60.0), "lux": (0.0, 2000.0), "water_l": (0.0, 20.0),
    "power_mains": (0.0, 260.0), "power_gen": (0.0, 260.0),
}


class FaultProfile:
    """Fault injection. Every field is a probability per interval, 0 = never.

    The point is to exercise the paths a healthy fleet never reaches: a device
    that only ever works proves nothing about the backend's error handling.
    """

    def __init__(self, *, drop_connectivity=0.0, delay_send=0.0,
                 invalid_reading=0.0, restart=0.0, delay_seconds=0.0):
        self.drop_connectivity = drop_connectivity
        self.delay_send = delay_send
        self.invalid_reading = invalid_reading
        self.restart = restart
        self.delay_seconds = delay_seconds

    @classmethod
    def from_env(cls, env=None) -> "FaultProfile":
        import os
        env = env if env is not None else os.environ

        def _f(name, default=0.0):
            try:
                return float(env.get(name, "") or default)
            except ValueError:
                return default
        return cls(
            drop_connectivity=_f("SIM_FAULT_DROP", 0.0),
            delay_send=_f("SIM_FAULT_DELAY", 0.0),
            invalid_reading=_f("SIM_FAULT_INVALID", 0.0),
            restart=_f("SIM_FAULT_RESTART", 0.0),
            delay_seconds=_f("SIM_FAULT_DELAY_SEC", 0.0),
        )

    def describe(self) -> str:
        on = [f"{k}={v:g}" for k, v in (
            ("drop", self.drop_connectivity), ("delay", self.delay_send),
            ("invalid", self.invalid_reading), ("restart", self.restart),
        ) if v]
        return ", ".join(on) if on else "none"


class DigitalTwin:
    """One simulated gateway, behaving as the requirements document asks.

    Lifecycle the orchestrator drives:
        start() → (retained desired arrives) → apply → report → tick() forever
    `tick()` publishes one telemetry window; `stop()` tears the session down.

    Everything the backend can observe — reports, command answers, telemetry,
    status, the Last Will — goes through the same topics and shapes a real
    device would use, so the backend's own validation is the test.
    """

    def __init__(self, unit_id: str, password: str, *, farm_id=None,
                 hardware_id: str | None = None, packs=("P-A", "P-B"),
                 host: str = "emqx", port: int = 8883, tls: bool = True,
                 interval_sec: float = 60.0, client_id_prefix: str = "twin",
                 faults: FaultProfile | None = None, seed: float | None = None):
        self.unit_id = unit_id
        self.password = password
        self.farm_id = farm_id
        # The pack resolver keys on (org, hardware_id, pack_id). The fleet
        # convention — and what the existing mappings were built with — is
        # "HW-" + the slot's own suffix.
        self.hardware_id = hardware_id or ("HW-" + unit_id[len("GW-"):])
        self.packs = list(packs)
        self.host = host
        self.port = port
        self.tls = tls
        self.interval_sec = interval_sec
        self.faults = faults or FaultProfile()
        self._seed = seed if seed is not None else random.random() * 6.28

        self.client_id = f"{client_id_prefix}-{unit_id}"

        # ---- device state (what a real unit would keep in flash) ----------
        self.applied_version = 0     # survives a reboot
        self.org_id = None
        self.tenant_farm_id = None
        self.sensors: list = []
        self.report_interval_sec = interval_sec
        self.state = "NEW"           # NEW → CONNECTED → CONFIGURED → ONLINE
        self.reboots = 0
        self._boot_ts = time.time()

        # ---- observability for the orchestrator and the operator ----------
        self.reports: list = []        # every body published on `reported`
        self.commands: list = []       # (state, uuid|action) decisions
        self.commands_seen: list = []  # every response body published
        self.telemetry_sent = 0
        self.faults_hit: list = []     # (kind, detail) for the log
        self.last_error: str | None = None
        self.connected = False

        self._client = None
        self._loop_started = False
        self._connect_rc = None
        self._stop = False
        self._last_device_ts = None   # keeps the dedup key unique
        # Scheduling state, owned by the twin rather than by the fleet. It lives
        # here because a twin that joins an already-running fleet must be
        # immediately schedulable: the fleet used to set these lazily, and a
        # freshly joined twin crashed the run loop with an AttributeError
        # before it ever sent a window.
        self.last_tick_at = 0.0
        self.last_reconnect_at = 0.0

    # ------------------------------------------------------------------ boot
    def start(self) -> int:
        """Connect and subscribe. Returns the CONNACK code (0 = accepted)."""
        import paho.mqtt.client as mqtt

        self._stop = False
        self._connect_rc = None
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            protocol=mqtt.MQTTv311,   # fleet pin
            clean_session=True,
        )
        client.username_pw_set(self.unit_id, self.password)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        if self.tls:
            import ssl
            context = ssl.create_default_context()
            # Internal-network broker: the docker hostname carries no public
            # SAN — the same accepted risk the backend's own subscribers carry.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            client.tls_set_context(context)

        # §3.5 — the Last Will, so an unclean death is visible as OFFLINE.
        # Only meaningful once bound: before that there is no status topic, and
        # registering a will on a denied topic would be refused by the broker.
        if self.org_id:
            client.will_set(
                self._status_topic(),
                json.dumps({"schema_version": SCHEMA_VERSION,
                            "slot_id": self.unit_id, "state": "OFFLINE"}),
                qos=1, retain=True,
            )
        client.connect(self.host, self.port, keepalive=30)
        client.loop_start()
        self._client = client
        self._loop_started = True

        deadline = time.time() + 5.0
        while time.time() < deadline and self._connect_rc is None:
            time.sleep(0.05)
        return -1 if self._connect_rc is None else self._connect_rc

    def stop(self) -> None:
        self._stop = True
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass
            self._client = None
            self._loop_started = False
        self.connected = False

    def reboot(self) -> None:
        """A power cycle: drop the session, keep flash, reconnect.

        `applied_version` deliberately survives — a real device re-applies
        nothing on boot and simply resumes reporting, which is what makes the
        server's monotonic guard meaningful.
        """
        self.reboots += 1
        self.stop()
        time.sleep(0.2)
        rc = self.start()
        if rc == 0:
            self._subscribe()
            self.state = "CONFIGURED" if self.applied_version else "CONNECTED"
            LOG.info("%s rebooted (applied_version=%d)", self.unit_id,
                     self.applied_version)

    # ------------------------------------------------------------- wire events
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = str(reason_code) in ("Success", "0")
        self._connect_rc = 0 if ok else 1
        self.connected = ok
        if ok:
            self.state = "CONFIGURED" if self.applied_version else "CONNECTED"
            self._subscribe()
            self.publish_status("ONLINE")
            LOG.info("%s connected (applied_version=%d)", self.unit_id,
                     self.applied_version)

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        self.connected = False
        if not self._stop:
            LOG.warning("%s lost its session — paho will retry", self.unit_id)

    def _on_message(self, client, userdata, msg):
        if msg.topic == desired_topic(self.unit_id):
            self._on_desired(msg.payload)
        elif msg.topic == command_topic(self.unit_id):
            self._on_command(msg.payload, lane="device")
        elif self.org_id and msg.topic == tenant_topic(
                self.org_id, self.tenant_farm_id, self.unit_id, "commands"):
            # §3.6 — after binding the same channel moves to the tenant lane.
            self._on_command(msg.payload, lane="tenant")

    def _subscribe(self) -> None:
        subs = [(desired_topic(self.unit_id), 1),
                (command_topic(self.unit_id), 1)]
        if self.org_id:
            subs.append((tenant_topic(self.org_id, self.tenant_farm_id,
                                      self.unit_id, "commands"), 1))
        self._client.subscribe(subs, qos=1)

    # ------------------------------------------------------- config apply (§3.3)
    def _on_desired(self, payload: bytes) -> None:
        try:
            doc = json.loads(payload)
        except (TypeError, ValueError) as exc:
            self.faults_hit.append(("MALFORMED_DOC", str(exc)))
            return

        # §3.3.1 — the document must be addressed to us
        if str(doc.get("slot_id") or "") != self.unit_id:
            self.faults_hit.append(("FOREIGN_SLOT", str(doc.get("slot_id"))))
            return

        try:
            version = int(doc.get("config_version") or 0)
        except (TypeError, ValueError):
            self.faults_hit.append(("BAD_VERSION", str(doc.get("config_version"))))
            return

        # §3.3.2 — monotonic guard: never apply an older or equal document
        if version <= self.applied_version:
            self.faults_hit.append(("STALE", f"{version} <= {self.applied_version}"))
            return

        # §4 — the document is complete; replace, never merge
        self.org_id = doc.get("org_id")
        self.tenant_farm_id = doc.get("farm_id", self.farm_id)
        self.sensors = doc.get("sensors") or []
        self.report_interval_sec = float(doc.get("report_interval_sec")
                                        or self.interval_sec)
        # §3.3.4 — persist before reporting
        self.applied_version = version
        self.state = "CONFIGURED"
        self._report("APPLIED", None)
        LOG.info("%s applied config v%d (%d sensors)", self.unit_id, version,
                 len(self.sensors))

    def _report(self, state: str, error) -> None:
        body = {
            "schema_version": SCHEMA_VERSION,
            "slot_id": self.unit_id,
            "config_version": self.applied_version,
            "state": state,
            "error": error,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self._publish(reported_topic(self.unit_id), body)
        self.reports.append(body)

    # ------------------------------------------------- command channel (§3.6)
    def _on_command(self, payload: bytes, lane: str) -> None:
        """Answer one envelope. Three events, never collapsed.

        Rules from the contract, in the order that matters:
          * an expired command is ANSWERED, not applied — silence would leave
            the server's journal in SENT until its own sweep expires it;
          * ACK, then act, then SUCCESS — three separate publishes, because the
            server's state machine records them separately;
          * an unknown action is a FAILED response with a reason.
        """
        try:
            envelope = json.loads(payload or b"{}")
        except (TypeError, ValueError):
            self.commands.append(("MALFORMED", payload[:64].decode("utf-8", "replace")))
            return

        command_uuid = str(envelope.get("command_uuid") or "")
        action = str(envelope.get("action") or "").upper()
        if not command_uuid:
            self.commands.append(("NO_UUID", action))
            return

        if self._expired(envelope.get("expires_at")):
            self._answer(command_uuid, "EXPIRED",
                         {"reason": "command expired on device"}, lane)
            self.commands.append(("EXPIRED", command_uuid))
            return

        handlers = {
            "REFRESH_CONFIG": self._cmd_refresh,
            "REBOOT": self._cmd_reboot,
            "DIAGNOSTICS_REQUEST": self._cmd_diagnostics,
            "ROTATE_CREDENTIAL": self._cmd_rotate,
        }
        handler = handlers.get(action)
        if handler is None:
            self._answer(command_uuid, "FAILED", None,
                         f"unknown action {action!r}", lane)
            self.commands.append(("UNKNOWN_ACTION", action))
            return

        self._answer(command_uuid, "ACK", {}, None, lane)
        try:
            result = handler(envelope.get("params") or {})
        except Exception as exc:  # noqa: BLE001 — a fault must not wedge the lane
            self._answer(command_uuid, "FAILED", None,
                         f"{type(exc).__name__}: {exc}", lane)
            self.commands.append(("FAILED", command_uuid))
            return
        self._answer(command_uuid, "SUCCESS", result, None, lane)
        self.commands.append(("SUCCESS", command_uuid))

    @staticmethod
    def _expired(expires_raw) -> bool:
        if not expires_raw:
            return False
        try:
            expires = datetime.fromisoformat(str(expires_raw))
        except (ValueError, TypeError):
            return False
        if expires.tzinfo is None:
            # No offset: assume UTC, never local. Treating a UTC stamp as local
            # on a +03:30 box makes every command look 3.5 hours expired.
            expires = expires.replace(tzinfo=timezone.utc)
        return expires < datetime.now(timezone.utc)

    def _answer(self, command_uuid: str, state: str, result,
                error=None, lane: str = "device") -> None:
        body = {
            "schema_version": SCHEMA_VERSION,
            "slot_id": self.unit_id,
            "command_uuid": command_uuid,
            "state": state,
        }
        if result is not None:
            body["result"] = result
        if error is not None:
            body["error"] = error
        if lane == "tenant" and self.org_id:
            topic = tenant_topic(self.org_id, self.tenant_farm_id,
                                 self.unit_id, "commands/response")
        else:
            topic = command_response_topic(self.unit_id)
        self._publish(topic, body)
        self.commands_seen.append(body)

    def _cmd_refresh(self, params: dict) -> dict:
        return {"applied_config_version": self.applied_version}

    def _cmd_reboot(self, params: dict) -> dict:
        self.reboots += 1
        return {"reboots": self.reboots}

    def _cmd_diagnostics(self, params: dict) -> dict:
        return {
            "uptime_s": max(0, int(time.time() - self._boot_ts)),
            "applied_config_version": self.applied_version,
            "reboots": self.reboots,
            "rssi": -71,
            "telemetry_sent": self.telemetry_sent,
        }

    def _cmd_rotate(self, params: dict) -> dict:
        """Adopt the new credential and PROVE it by presenting it.

        Two things this deliberately does NOT do:
          * it does not reconnect — the broker still holds the OLD password
            until the server commits, so reconnecting now is refused and the
            device takes itself offline over a rotation;
          * it does not drop the old password until the server confirms.
        The SUCCESS response carries the password because that IS the proof.
        """
        new_password = str(params.get("new_password") or "")
        if not new_password:
            raise ValueError("rotate without a new password")
        self.password = new_password
        return {"password": new_password, "rotated": True, "reconnect": "deferred"}

    # ------------------------------------------------------ telemetry (§3.8)
    def tick(self) -> bool:
        """Publish one telemetry window. Returns False if it could not send."""
        if not self.org_id:
            return False  # unbound: no tenant topic to publish on yet

        if self.faults.drop_connectivity and random.random() < self.faults.drop_connectivity:
            self.faults_hit.append(("DROP", "skipping this window"))
            return False
        if self.faults.restart and random.random() < self.faults.restart:
            self.faults_hit.append(("RESTART", "power cycle"))
            self.reboot()
            return False
        if self.faults.delay_send and random.random() < self.faults.delay_send:
            time.sleep(self.faults.delay_seconds or 5.0)
            self.faults_hit.append(("DELAY", f"{self.faults.delay_seconds:g}s"))

        body = self._telemetry_payload()
        ok = self._publish(
            tenant_topic(self.org_id, self.tenant_farm_id, self.unit_id, "telemetry"),
            body, wait=True)
        if ok:
            self.telemetry_sent += 1
        return ok

    def _telemetry_payload(self) -> dict:
        """Schema 2.0 — the shape the ingest pipeline actually accepts.

        Three things the pipeline gates on and a naive simulator gets wrong:
          * `schema_version` must be "2.0" (Gate 3 rejects anything else);
          * `gateway_slot_id` must equal the topic's slot (Gate 2);
          * every reading's `pack_id` must resolve through
            (org, gateway_hardware_id, pack_id) or the whole message is
            dead-lettered as UNKNOWN_SLOT (Gate 5).
        """
        now = datetime.now(timezone.utc)
        t = time.time() - self._boot_ts
        device_ts = self._monotonic_ts(now)
        readings = []
        for index, pack_id in enumerate(self.packs):
            metrics = self._metrics(t + index * 1.7)
            if self.faults.invalid_reading and random.random() < self.faults.invalid_reading:
                # A real sensor fault: a value outside any physical range.
                bad = random.choice(list(metrics))
                metrics[bad] = METRIC_RANGE[bad][1] * 3.0
                self.faults_hit.append(("INVALID", f"{bad}={metrics[bad]}"))
            readings.append({"pack_id": pack_id, "metrics": metrics})
        return {
            "schema_version": TELEMETRY_SCHEMA,
            "gateway_slot_id": self.unit_id,
            "gateway_hardware_id": self.hardware_id,
            # Unique per window: this is also the dedup component.
            "device_ts": device_ts,
            "readings": readings,
        }

    def _monotonic_ts(self, now: datetime) -> str:
        """A device_ts that never repeats, even inside one clock microsecond.

        The dedup key is `dedup:{org}:{slot}:{device_ts}`, so two windows with
        the same stamp are indistinguishable from a redelivery and the second is
        silently dropped. A burst — a delayed send catching up, say — could
        otherwise collide. The stamp still tracks the real clock; it only ever
        moves forward by the smallest step needed.
        """
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        if self._last_device_ts is not None and stamp <= self._last_device_ts:
            bumped = datetime.strptime(self._last_device_ts,
                                       "%Y-%m-%dT%H:%M:%S.%fZ") + timedelta(microseconds=1)
            stamp = bumped.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        self._last_device_ts = stamp
        return stamp

    def _metrics(self, t: float) -> dict:
        """A diurnal curve per metric, offset per pack so packs differ.

        Plausible and in range, not a thermal simulation — see the limitations
        note in the module docstring.
        """
        out = {}
        for name, base in METRIC_BASE.items():
            value = base + METRIC_AMPLITUDE[name] * math.sin(t / 300.0 + self._seed)
            lo, hi = METRIC_RANGE[name]
            out[name] = round(min(hi, max(lo, value)), 2)
        return out

    # ------------------------------------------------------------- status (§3.5)
    def _status_topic(self) -> str:
        if self.org_id:
            return tenant_topic(self.org_id, self.tenant_farm_id,
                                self.unit_id, "status")
        # Unbound: there is no tenant topic yet. The device lane has no status
        # topic in the contract, so an unbound twin simply stays quiet rather
        # than inventing one.
        return tenant_topic("unbound", 0, self.unit_id, "status")

    def publish_status(self, state: str = "ONLINE") -> bool:
        """Publish ONLINE/OFFLINE on the tenant status lane.

        An unbound device has no tenant topic (and the contract defines no
        device-lane status topic), so it stays quiet rather than inventing one
        — a publish the ACL would deny anyway.
        """
        if not self.org_id:
            return False
        body = {"schema_version": SCHEMA_VERSION, "slot_id": self.unit_id,
                "state": state}
        return self._publish(self._status_topic(), body, retain=True)

    # ------------------------------------------------------------------ helper
    def _publish(self, topic: str, body: dict, *, retain: bool = False,
                 wait: bool = False) -> bool:
        if self._client is None:
            self.last_error = "no MQTT client"
            return False
        info = self._client.publish(topic, json.dumps(body), qos=1, retain=retain)
        if wait:
            try:
                info.wait_for_publish(timeout=5.0)
            except Exception:  # noqa: BLE001
                return False
        if info.rc != 0:
            self.last_error = f"publish rc={info.rc}"
            return False
        return True

    # ------------------------------------------------------------------ status
    def summary(self) -> str:
        return (f"{self.unit_id} state={self.state} applied_v={self.applied_version} "
                f"org={'yes' if self.org_id else 'no'} sent={self.telemetry_sent} "
                f"cmds={len(self.commands_seen)} reboots={self.reboots} "
                f"faults={len(self.faults_hit)}")
