#!/usr/bin/env python3
"""Simulated gateways — publishes schema-2.0 telemetry for provisioned SIM slots.

Reads gateway credentials from /sim/creds.json (minted by the review-approve
flow, never hand-made). One MQTT connection per gateway, TLS to emqx:8883.

Payload satisfies every pipeline gate (pipeline.py):
  topic {org}/farms/{farm}/gateways/{slot}/telemetry  (T1 shape)
  gateway_slot_id == topic slot (identity), schema_version 2.0,
  unique device_ts (dedup), known (hardware, pack) mappings.
"""

import json
import math
import os
import ssl
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

BROKER = os.environ.get("MQTT_HOST", "emqx")
PORT = int(os.environ.get("MQTT_PORT", "8883"))
CA = os.environ.get("MQTT_TLS_CA", "/app/certs/ca.crt")
ORG = os.environ["SIM_ORG_UUID"]
INTERVAL = float(os.environ.get("SIM_INTERVAL_SEC", "10"))
PACKS = ("P-A", "P-B")

BASE_METRICS = {
    "temp": 24.5, "humidity": 62.0, "nh3": 8.0, "co": 4.0,
    "lux": 320.0, "water_l": 1.2, "power_mains": 220.0, "power_gen": 0.0,
}
AMPLITUDE = {
    "temp": 1.5, "humidity": 5.0, "nh3": 2.0, "co": 1.0,
    "lux": 40.0, "water_l": 0.4, "power_mains": 3.0, "power_gen": 0.0,
}


def metrics(t, seed):
    out = {}
    for k, base in BASE_METRICS.items():
        out[k] = round(base + AMPLITUDE[k] * math.sin(t / 300.0 + seed), 2)
    return out


def make_client(slot, password):
    # Owner decision 2026-10-18 (Tracker §4t): fleet protocol = MQTT 3.1.1
    # (was the only MQTTv5-pinned client in the repo).
    c = mqtt.Client(client_id=f"sim-{slot}", protocol=mqtt.MQTTv311)
    c.username_pw_set(slot, password)
    c.tls_set(ca_certs=CA, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    c.tls_insecure_set(True)  # staging-only, same posture as subscribers
    c.connect(BROKER, PORT, keepalive=60)
    return c


def main():
    creds = json.load(open("/sim/creds.json"))
    clients = {}
    for slot, c in creds.items():
        hw = "HW-" + slot[len("GW-"):]
        clients[slot] = (make_client(slot, c["password"]), c["farm_id"], hw)
        print(f"connected sim gateway {slot} (farm {c['farm_id']})", flush=True)
    for c, _, _ in clients.values():
        c.loop_start()
    t0 = time.time()
    seed = {s: i * 1.7 for i, s in enumerate(clients)}
    while True:
        t = time.time() - t0
        for slot, (client, farm_id, hw) in clients.items():
            payload = {
                "schema_version": "2.0",
                "gateway_slot_id": slot,
                "gateway_hardware_id": hw,
                "device_ts": datetime.now(timezone.utc).isoformat(),
                "readings": [
                    {"pack_id": p, "metrics": metrics(t, seed[slot] + j)}
                    for j, p in enumerate(PACKS)
                ],
            }
            topic = f"{ORG}/farms/{farm_id}/gateways/{slot}/telemetry"
            client.publish(topic, json.dumps(payload), qos=1)
        print(f"published 1 msg x {len(clients)} gateways", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
