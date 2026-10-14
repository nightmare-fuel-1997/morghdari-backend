"""Watch test: fire ONE temp alert, then observe it untouched for 10 min.
Snapshots every 60s: status, blob, breach key. Detects spontaneous actors."""
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

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

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

def snap(label):
    rows = [l for l in psql(
        f"SELECT substr(id::text,1,8)||'|'||metric_type||'|'||current_status FROM alert_instances "
        f"WHERE org_id='{ORG}' AND saloon_id=1 AND metric_type='temp' ORDER BY triggered_at DESC LIMIT 2"
    ).splitlines() if l.strip()]
    print(f"{label} rows={rows} blob={bool(redis('GET', 'alert:active:saloon:1:temp'))} "
          f"bkey_ttl={redis('TTL', 'saloon:1:breach:temp')}", flush=True)

ar = [l for l in psql("SELECT id FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").splitlines() if l.strip()]
print("start active:", ar, flush=True)
for _ in range(6):
    pub(31.0); time.sleep(8)
for i in range(11):
    snap(f"t+{i}m")
    time.sleep(60)
print("WATCH DONE", flush=True)
