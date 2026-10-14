"""Wave 7a — 20.2 feeder: sustained temp breach at PROD duration=900 (~17 min).
Run in background; wave7b checks the alert + ack + resolve afterwards."""
import json, os, re, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import tok, claims
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
PW = "password12345"
OWNER = tok("demo-owner", PW)
ORG = claims(OWNER)["org_id"]
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(temp):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": "P-A", "metrics": {
             "temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
             "humidity": {"avg": 58.0},
             "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/1/gateways/GW-QA0001/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

n = 0
t0 = time.time()
while time.time() - t0 < 17 * 60:
    pub(31.0)
    n += 1
    if n % 5 == 0:
        print(f"fed {n}", flush=True)
    time.sleep(40)
print(f"FEED DONE n={n}", flush=True)
