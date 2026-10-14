"""Heartbeat all demo packs once (recovery helper)."""
import json, os, re, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import tok, claims
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
OWNER = tok("demo-owner", "password12345")
ORG = claims(OWNER)["org_id"]
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(slot, hw, pack, farm):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": hw, "gateway_slot_id": slot, "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": pack, "metrics": {
             "temp": {"avg": 26.5, "min": 25.5, "max": 27.5}, "humidity": {"avg": 58.0},
             "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/{farm}/gateways/{slot}/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

for _ in range(2):
    for pack in ("P-A", "P-B", "P-C"):
        pub("GW-QA0001", "HWM-QA01", pack, 1)
        pub("GW-QA0002", "HWM-QA02", pack, 2)
    time.sleep(10)
print("HB DONE", flush=True)
