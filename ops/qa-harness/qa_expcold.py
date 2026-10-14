"""Isolation: does temp=10 (cold side) REALLY fire? Board is clean."""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
env1 = {}
for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8"):
    m = re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())
    if m:
        env1[m.group(1)] = m.group(2)

OWNER = tok("demo-owner", "password12345")
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

before = subprocess.run(["docker", "exec", "morghdari-postgres", "psql", "-U", env1["POSTGRES_ADMIN_USER"],
    "-d", env1["DATABASE_NAME"], "-w", "-t", "-A", "-c", "SELECT count(*) FROM alert_instances"],
    capture_output=True, text=True, env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()
print("alerts before:", before, flush=True)
for i in range(4):
    pub(10.0)
    print(f"pub {i+1}", flush=True)
    time.sleep(8)
time.sleep(45)
after = subprocess.run(["docker", "exec", "morghdari-postgres", "psql", "-U", env1["POSTGRES_ADMIN_USER"],
    "-d", env1["DATABASE_NAME"], "-w", "-t", "-A", "-c",
    "SELECT substr(id::text,1,8), metric_type, current_status, current_value FROM alert_instances ORDER BY triggered_at DESC LIMIT 3"],
    capture_output=True, text=True, env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()
print("latest rows:\n" + after, flush=True)
print("EXP DONE", flush=True)
