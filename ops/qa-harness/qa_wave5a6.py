"""Wave 5a6 — CLEAN redo of 8.10b, 8.11, 8.14 (mute confound removed)."""
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
    assert r.returncode == 0, "shell failed: " + (r.stderr or "")[-200:]
    return r

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
ORG = OW["org_id"]
MGR = tok("manager1", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(temp, hum=58.0, pack="P-A", hw="HWM-QA01", slot="GW-QA0001", farm=1):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": hw, "gateway_slot_id": slot, "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": pack, "metrics": {
             "temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
             "humidity": {"avg": hum},
             "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/{farm}/gateways/{slot}/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def M26():
    return None

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

def resolve_all():
    for _ in range(3):
        pub(26.5); time.sleep(8)
    sweep_org(); time.sleep(3); sweep_org()

# ---------- preflight: clean board, no mutes, temp_max=30, no grace ----------
ar = [l for l in psql("SELECT id FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
mm = psql("SELECT count(*) FROM maintenance_mutes").stdout.strip()
sm = psql("SELECT count(*) FROM alert_subscriptions").stdout.strip()
tm = psql("SELECT max_value FROM lifecycle_threshold_rules WHERE metric='temp'").stdout.strip()
g = redis("GET", "threshold:transition:1")
R.ok("8.0pre", ar == [] and mm == "0" and sm == "0" and tm == "30.00" and not g,
     f"active={ar} mutes={mm} subs={sm} temp_max={tm} grace={g!r}")

# ---------- re-arm grace: 30 -> 35 ----------
rid = psql("SELECT id FROM lifecycle_threshold_rules WHERE metric='temp' LIMIT 1").stdout.strip()
assert api("PUT", f"/thresholds/{rid}/", token=MGR, body={"max": 35})["status"] == 200
djshell("from apps.alerts.services.thresholds import recache_all_saloons\nprint(recache_all_saloons())")
assert redis("GET", "threshold:transition:1"), "grace not written!"
# ---------- 8.10b ramp (deltas <5 dodge velocity) ----------
for _ in range(2):
    pub(26.5); time.sleep(12)
for tv in (31.0, 34.0):
    for _ in range(2):
        pub(tv); time.sleep(12)
before = idset("temp", 1)
for _ in range(5):
    pub(36.0); time.sleep(9)
time.sleep(35)
new = idset("temp", 1) - before
R.ok("8.10b", new == set(), f"breach inside grace suppressed; new={len(new)}")
# ---------- restore 30 + clear grace ----------
assert api("PUT", f"/thresholds/{rid}/", token=MGR, body={"max": 30})["status"] == 200
djshell("from apps.alerts.services.thresholds import recache_all_saloons\nprint(recache_all_saloons())")
redis("DEL", "threshold:transition:1")
# ---------- 8.11 THI ramp (step down first: cache holds 36.0) ----------
for tv in (32.0, 28.0, 26.5):
    pub(tv); time.sleep(25)
before = idset("thi", 1)
for tv, hu in [(28.0, 66.0), (30.0, 70.0), (32.0, 74.0)]:
    pub(tv, hu); time.sleep(30)
for _ in range(4):
    pub(35.0, 78.0); time.sleep(8)
new = wait_new(before, "thi", 1, 120)
R.ok("8.11", len(new) >= 1, f"derived THI breached n={len(new)}")
resolve_all()
# ---------- 8.14 concurrent ----------
before = idset("temp", 1)
for _ in range(4):
    pub(31.0, pack="P-A"); pub(31.0, pack="P-B"); time.sleep(8)
new = wait_new(before, "temp", 1, 120)
R.ok("8.14", len(new) == 1, f"two packs -> exactly one alert n={len(new)}")
resolve_all()
R.f.flush()
print("WAVE5A6 DONE", flush=True)
