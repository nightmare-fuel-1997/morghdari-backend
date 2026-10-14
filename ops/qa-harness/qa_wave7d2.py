"""Wave 7d2 — 20.1c redo with committed credential + acked publish."""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec
import paho.mqtt.client as mqtt

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
PW = "password12345"
OWNER = tok("demo-owner", PW)
ORG = claims(OWNER)["org_id"]
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()

slot = "GW-84E0A7A50FF6F063"
sal = psql(f"SELECT farm_id FROM gateway_slots WHERE slot_id='{slot}'")
hw = psql(f"SELECT hardware_id FROM gateway_hardware_registry WHERE slot_id='{slot}' AND removed_at IS NULL LIMIT 1")
dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.tls_set(ca_certs=CA)
c.username_pw_set(slot, "KFm1z4-BnrsFun0pYUbGuUKEQ29O8_uhnbcS_BDomHk")
c.connect("localhost", 8883, 10)
c.loop_start()
time.sleep(0.8)
info = c.publish(f"{ORG}/farms/{sal}/gateways/{slot}/telemetry", json.dumps(
    {"gateway_hardware_id": hw, "gateway_slot_id": slot, "schema_version": "2.0",
     "device_ts": dts, "is_buffered": False, "clock_drift": False,
     "readings": [{"pack_id": "P-A", "metrics": {
         "temp": {"avg": 26.5, "min": 25.5, "max": 27.5}, "humidity": {"avg": 58.0},
         "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}).encode(), qos=1)
try:
    info.wait_for_publish(timeout=10)
    ok = info.is_published()
except Exception:
    ok = False
time.sleep(0.5)
c.loop_stop(); c.disconnect()
time.sleep(15)
cnt = psql(f"SELECT count(*) FROM saloon_telemetry WHERE device_ts='{dts}'")
R.ok("20.1c", ok and cnt == "1", f"acked={ok} rows={cnt} (device path provisioned by the onboard chain)")
R.f.flush()
print("WAVE7D2 DONE", flush=True)
