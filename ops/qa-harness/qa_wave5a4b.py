"""Wave 5a4b — 8.10b redo: ramp into breach (deltas <5C dodge velocity)."""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec
import paho.mqtt.client as mqtt

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
ORG = OW["org_id"]
MGR = tok("manager1", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")

ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])
def djshell(py):
    r = subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                        "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                       capture_output=True, text=True)
    assert r.returncode == 0, "shell failed: " + (r.stderr or "")[-200:]
    return r

rid = psql("SELECT id FROM lifecycle_threshold_rules WHERE metric='temp' LIMIT 1").stdout.strip()
r = api("PUT", f"/thresholds/{rid}/", token=MGR, body={"max": 35})
djshell("from apps.alerts.services.thresholds import recache_all_saloons\nprint(recache_all_saloons())")
g = redis("GET", "threshold:transition:1")
assert r["status"] == 200 and g, f"grace setup failed PUT={r['status']} key={g!r}"
print("grace re-armed, temp_max=35", flush=True)

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

def idset():
    return set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND saloon_id=1 AND metric_type='temp'"
    ).stdout.strip().splitlines() if l.strip())

def blob_reason():
    b = redis("GET", "alert:active:saloon:1:temp")
    try: return json.loads(b or "{}").get("reason", "")
    except Exception: return b or ""

# temp_max is 35 (from 8.10a), grace key live. ramp: 26.5 -> 31 -> 34 -> 36
for _ in range(2):
    pub(26.5); time.sleep(12)
for tv in (31.0, 34.0):
    for _ in range(2):
        pub(tv); time.sleep(12)
before = idset()
for _ in range(5):
    pub(36.0); time.sleep(9)
time.sleep(35)
new = idset() - before
reason = blob_reason() if new else ""
R.ok("8.10b", new == set(), f"breach inside grace suppressed; new={len(new)} reason={reason!r}")
R.f.flush()
print("WAVE5A4B DONE", flush=True)
