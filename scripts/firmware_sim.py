#!/usr/bin/env python
"""firmware_sim.py — protocol-level gateway simulator for the bootstrap lane.

INDEPENDENCE RULE (do not remove): this file MUST NOT import anything from
`apps/` or `morghdari/`. It implements the contract in
`docs/hardware-firmware-requirements-v1.md` from scratch, using only the topic
strings and payload shapes written there. The moment it imports the backend's
own `topics.py` or `desired_state.py`, a bug in our grammar stops being caught
by the end-to-end run — the test becomes a tautology. Keep it that way, even
when it is tempting to "just reuse" a constant.

فارسی: شبیه‌سازِ پروتکل — پیاده‌سازی مستقلِ قراردادِ فریم‌ویر؛ هیچ واردکردنی از
پشت‌براک ندارد تا باگ‌های خودمان را بگیرد.

Used by:
  * scripts/e2e_bootstrap_lane.py — the live assertion run in CI
  * the firmware team — a readable reference implementation of §3/§4

Import-safe: paho is imported lazily inside connect(), so `import firmware_sim`
never needs paho installed.
"""

from __future__ import annotations

import json
import logging
import queue
import time

LOG = logging.getLogger("firmware_sim")

# ---- the contract, transcribed by hand from the requirements document ---------
SCHEMA_VERSION = "1.0"


def desired_topic(unit_id: str) -> str:
    return f"devices/{unit_id}/desired"


def command_topic(unit_id: str) -> str:
    return f"devices/{unit_id}/command"


def reported_topic(unit_id: str) -> str:
    return f"devices/{unit_id}/reported"


def tenant_topic(org_id: str, farm_id: int, slot_id: str, suffix: str) -> str:
    return f"{org_id}/farms/{farm_id}/gateways/{slot_id}/{suffix}"


class SimulatedGateway:
    """A device that behaves exactly as the requirements document asks.

    The e2e run drives it through: connect → subscribe → receive retained
    config → apply → report. Everything the document specifies as device
    behaviour lives here, so a failing check points at either the firmware
    contract or the backend, never at a mock.
    """

    def __init__(self, unit_id: str, password: str, *, host: str, port: int = 8883,
                 tls: bool = True, protocol: int = 4, client_id_prefix: str = "sim"):
        """`protocol` is the MQTT WIRE version: 3 = 3.1, 4 = 3.1.1, 5 = 5.0.

        4 is the fleet default (hardware-team constraint). 5 is accepted only so
        a test can prove the broker refuses it — see the live test.
        """
        import paho.mqtt.client as mqtt

        self.unit_id = unit_id
        self.password = password
        self.host = host
        self.port = port
        self.tls = tls
        self.protocol = protocol
        self.client_id = f"{client_id_prefix}-{unit_id}"

        self.applied_version = 0          # persists across "reboots"
        self.state = {}                   # what the device currently believes
        self.faults = []                  # refused documents, for assertions
        self.published = []               # every publish() result, for assertions

        self._client = None
        self._loop_started = False
        self._connect_rc = None
        self.reports = queue.Queue()      # what we published on `reported`
        self.received = queue.Queue()     # every inbound message, for assertions

    # ------------------------------------------------------------------ wire
    def connect(self) -> int:
        """Connect. Returns the CONNACK result code (0 = accepted)."""
        import paho.mqtt.client as mqtt

        protocol = {
            3: mqtt.MQTTv31,
            4: mqtt.MQTTv311,
            5: mqtt.MQTTv5,
        }.get(self.protocol)
        if protocol is None:
            raise ValueError(f"unsupported MQTT protocol version: {self.protocol}")

        # MQTT 5.0 dropped `clean_session` in favour of `clean_start`, and paho
        # raises outright if both are set. The fleet is 3.1.1, so the 5.0 branch
        # exists only so the live test can prove the broker REFUSES such a client.
        kwargs = {
            "callback_api_version": mqtt.CallbackAPIVersion.VERSION2,
            "client_id": self.client_id,
            "protocol": protocol,
        }
        if protocol != mqtt.MQTTv5:
            kwargs["clean_session"] = True
        self._client = mqtt.Client(**kwargs)
        self._client.username_pw_set(self.unit_id, self.password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        if self.tls:
            import ssl

            context = ssl.create_default_context()
            # Internal-network broker: the docker hostname carries no public SAN,
            # the same accepted risk the backend's subscribers carry.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self._client.tls_set_context(context)

        self._client.connect(self.host, self.port, keepalive=30)
        self._client.loop_start()
        self._loop_started = True
        # wait briefly for CONNACK; 0 == accepted
        deadline = time.time() + 5.0
        while time.time() < deadline and self._connect_rc is None:
            time.sleep(0.05)
        return -1 if self._connect_rc is None else self._connect_rc

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            self._loop_started = False

    def subscribe(self) -> None:
        """§3.2 — the three topics, all derived from our own unit_id."""
        self._client.subscribe(
            [
                (desired_topic(self.unit_id), 1),
                (command_topic(self.unit_id), 1),
            ],
            qos=1,
        )

    def try_publish_tenant_telemetry(self, org_id="deadbeef", farm_id=1) -> bool:
        """Attempt a publish that an UNBOUND device must never be allowed.

        Used by the e2e run to prove the ACL is a real deny-by-default and not
        just a row that looks right in a table.
        """
        topic = tenant_topic(org_id, farm_id, self.unit_id, "telemetry")
        info = self._client.publish(topic, '{"probe": true}', qos=1)
        try:
            info.wait_for_publish(timeout=3.0)
        except Exception:  # noqa: BLE001
            return False
        return bool(info.is_published())

    # ------------------------------------------------------------- behaviour
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connect_rc = 0 if str(reason_code) in ("Success", "0") else 1

    def _on_message(self, client, userdata, msg):
        self.received.put((msg.topic, msg.payload))
        if msg.topic == desired_topic(self.unit_id):
            self._on_desired(msg.payload)
        # command messages are out of scope for v1 of the simulator

    def _on_desired(self, payload: bytes) -> None:
        """§3.3 — validate, monotonic-guard, apply, persist, report."""
        try:
            doc = json.loads(payload)
        except (TypeError, ValueError) as exc:
            self.faults.append(("MALFORMED", str(exc)))
            return

        # §3.3.1 — the document must be addressed to us
        if str(doc.get("slot_id") or "") != self.unit_id:
            self.faults.append(("FOREIGN_SLOT", str(doc.get("slot_id"))))
            return

        try:
            version = int(doc.get("config_version") or 0)
        except (TypeError, ValueError):
            self.faults.append(("BAD_VERSION", str(doc.get("config_version"))))
            return

        # §3.3.2 — monotonic guard: never apply an older or equal document
        if version <= self.applied_version:
            self.faults.append(("STALE", f"{version} <= {self.applied_version}"))
            return

        # §4 — the document is complete; replace, never merge
        self.state = {
            "org_id": doc.get("org_id"),
            "farm_id": doc.get("farm_id"),
            "sensors": doc.get("sensors"),
            "topics": doc.get("topics"),
            "report_interval_sec": doc.get("report_interval_sec"),
        }

        # §3.3.4 — persist before reporting
        self.applied_version = version

        self._report("APPLIED", None)

    def _report(self, state: str, error) -> None:
        body = {
            "schema_version": SCHEMA_VERSION,
            "slot_id": self.unit_id,
            "config_version": self.applied_version,
            "state": state,
            "error": error,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._client.publish(
            reported_topic(self.unit_id), json.dumps(body), qos=1
        )
        self.reports.put(body)

    def wait_for_config(self, timeout: float = 10.0) -> dict | None:
        """Block until a config has been applied. Returns the report body."""
        try:
            return self.reports.get(timeout=timeout)
        except queue.Empty:
            return None

    def publish_probe_report(self) -> bool:
        """Publish on our own granted report lane, and say whether it landed.

        A rule row that exists is not the same as a permission the broker
        grants, so the e2e run publishes a real message here: if the broker
        refuses it there is no PUBACK, `wait_for_publish` fails, and the check
        fails instead of passing on the shape of the table.
        """
        import paho.mqtt.client as mqtt

        body = {
            "schema_version": SCHEMA_VERSION,
            "slot_id": self.unit_id,
            "config_version": self.applied_version,
            "state": "PROBE",
            "error": None,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        info = self._client.publish(
            reported_topic(self.unit_id), json.dumps(body), qos=1
        )
        self.published.append(info)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        try:
            info.wait_for_publish(timeout=5.0)
        except Exception:  # noqa: BLE001
            return False
        return bool(info.is_published())
