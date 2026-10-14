"""Wave 5a4 — 8.9/8.9b velocity, 8.10 grace. ID-set oracles."""
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

ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])
def djshell(py):
    r = subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                        "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                       capture_output=True, text=True)
    assert r.returncode == 0, "shell failed: " + (r.stderr or "")[-300:]
    return r

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
ORG = OW["org_id"]
MGR = tok("manager1", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(payload, farm=1):
    slot = payload["gateway_slot_id"]
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/{farm}/gateways/{slot}/telemetry", json.dumps(payload).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def M(temp=26.5, nh3=12.0, hum=58.0, nh3_ts=0):
    return {"temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
            "humidity": {"avg": hum},
            "nh3": {"avg": nh3, "min": max(0.0, nh3 - 1.0), "max": nh3 + 1.0, "max_ts": nh3_ts}}

def P(pack="P-A", hw="HWM-QA01", slot="GW-QA0001", metrics=None, dts=None):
    return {"gateway_hardware_id": hw, "gateway_slot_id": slot, "schema_version": "2.0",
            "device_ts": dts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "is_buffered": False, "clock_drift": False,
            "readings": [{"pack_id": pack, "metrics": metrics or M()}]}

def idset(metric=None, saloon=None):
    q = f"SELECT id FROM alert_instances WHERE org_id='{ORG}'"
    if metric: q += f" AND metric_type='{metric}'"
    if saloon: q += f" AND saloon_id={saloon}"
    return set(l.strip() for l in psql(q).stdout.strip().splitlines() if l.strip())

def wait_new(before, metric=None, saloon=None, timeout=150):
    t0 = time.time()
    while time.time() - t0 < timeout:
        diff = idset(metric, saloon) - before
        if diff: return diff
        time.sleep(5)
    return set()

def sweep_org():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import escalate_all\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('esc=', escalate_all())").stdout.strip()

def blob_reason(saloon, metric):
    b = redis("GET", f"alert:active:saloon:{saloon}:{metric}")
    try: return json.loads(b or "{}").get("reason", "")
    except Exception: return b or ""

ar = [l for l in psql("SELECT id FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
assert ar == [], f"board not clean: {ar}"

for _ in range(2):
    pub(P(metrics=M(temp=26.5))); time.sleep(9)
before = idset("temp", 1)
pub(P(metrics=M(temp=32.0)))
new = wait_new(before, "temp", 1, 120)
R.ok("8.9", len(new) >= 1 and "rate-of-change" in blob_reason(1, "temp") and "5.0" in blob_reason(1, "temp"),
     f"reason={blob_reason(1, 'temp')!r}")
sweep_org()
for _ in range(2):
    pub(P(metrics=M())); time.sleep(8)
sweep_org()
pub(P(pack="P-A", hw="HWM-QA02", slot="GW-QA0002", metrics=M(temp=24.0)), farm=2)
time.sleep(10)
before = idset("temp", 2)
pub(P(pack="P-A", hw="HWM-QA02", slot="GW-QA0002", metrics=M(temp=28.9)), farm=2)
time.sleep(50)
R.ok("8.9b", idset("temp", 2) - before == set(), "delta 4.9 under max: no velocity, no threshold")

rid = psql("SELECT id FROM lifecycle_threshold_rules WHERE metric='temp' LIMIT 1").stdout.strip()
r = api("PUT", f"/thresholds/{rid}/", token=MGR, body={"max": 35})
djshell("from apps.alerts.services.thresholds import recache_all_saloons; print(recache_all_saloons())")
g = redis("GET", "threshold:transition:1")
R.ok("8.10a", r["status"] == 200 and bool(g), f"PUT={r['status']} grace written")
before = idset("temp", 1)
for _ in range(4):
    pub(P(metrics=M(temp=36.0))); time.sleep(8)
time.sleep(30)
R.ok("8.10b", idset("temp", 1) - before == set(), "breach inside 300s grace suppressed")
before = idset("nh3", 1)
pub(P(metrics=M(nh3=59.0, nh3_ts=59)))
R.ok("8.10c", len(wait_new(before, "nh3", 1, 100)) >= 1, "velocity bypasses grace")
sweep_org()
for _ in range(2):
    pub(P(metrics=M())); time.sleep(8)
sweep_org()
r = api("PUT", f"/thresholds/{rid}/", token=MGR, body={"max": 30})
djshell("from apps.alerts.services.thresholds import recache_all_saloons; print(recache_all_saloons())")
redis("DEL", "threshold:transition:1")
R.ok("8.10d", r["status"] == 200, "temp_max restored to 30, grace cleared for lane isolation")
R.f.flush()
print("WAVE5A4 DONE", flush=True)
