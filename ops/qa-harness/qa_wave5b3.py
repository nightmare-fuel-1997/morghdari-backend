"""Wave 5b3 — 9.7 redo (poll key-gone first), 9.8 redo, 9.12. Lag-tolerant."""
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
ENG = tok("engineer1", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(temp=None, nh3=None):
    metrics = {"temp": {"avg": 26.5, "min": 25.5, "max": 27.5}, "humidity": {"avg": 58.0},
               "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}
    if temp is not None:
        metrics["temp"] = {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)}
    if nh3 is not None:
        metrics["nh3"] = nh3
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": "P-A", "metrics": metrics}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/1/gateways/GW-QA0001/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def idset(metric=None):
    q = f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND saloon_id=1"
    if metric: q += f" AND metric_type='{metric}'"
    return set(l.strip() for l in psql(q).stdout.strip().splitlines() if l.strip())

def active_ids():
    return set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND current_status LIKE 'ACTIVE%'"
    ).stdout.strip().splitlines() if l.strip())

def status_of(aid):
    return psql(f"SELECT current_status FROM alert_instances WHERE id='{aid}'").stdout.strip()

def sweep_org():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import escalate_all\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('esc=', escalate_all())").stdout.strip()

def wait_key_gone(key, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not redis("EXISTS", key) == "1":
            return True
        time.sleep(5)
    return False

def wait_new(before, metric=None, timeout=150):
    t0 = time.time()
    while time.time() - t0 < timeout:
        diff = idset(metric) - before
        if diff: return diff
        time.sleep(5)
    return set()

# ---------- 9.7: the two ACTIVE alerts (temp+thi from 9.8 setup) ----------
aids = sorted(active_ids())
assert len(aids) == 2, f"expected exactly the 2 setup alerts, got {aids}"
for _ in range(4):
    pub(26.5); time.sleep(8)
R.ok("9.7a", wait_key_gone("saloon:1:breach:temp") and wait_key_gone("saloon:1:breach:thi"),
     "breach keys cleared by sustained normal values")
sweep_org(); time.sleep(3); sweep_org()
st = {a: status_of(a) for a in aids}
blob = redis("KEYS", "alert:active:saloon:1:*")
R.ok("9.7", all(s == "RESOLVED" for s in st.values()) and not blob.strip(),
     f"statuses={st} blobs={blob!r}")

# ---------- 9.8: fresh temp alert, ack it, spike NH3 ----------
before = idset("temp")
for _ in range(6):
    pub(31.0); time.sleep(8)
new = wait_new(before, "temp", 150)
assert new, "9.8 setup failed"
aid8 = sorted(new)[0]
assert api("POST", f"/alerts/{aid8}/acknowledge/", token=ENG)["status"] == 200
before_nh = idset("nh3")
pub(26.5, nh3={"avg": 59.0, "min": 58.0, "max": 60.0, "max_ts": 59})
new_nh = wait_new(before_nh, "nh3", 120)
R.ok("9.8", len(new_nh) >= 1, "velocity pages despite ack pause on temp alert")
for _ in range(3):
    pub(26.5); time.sleep(8)
sweep_org(); time.sleep(3); sweep_org()

# ---------- 9.12 replay exclusion ----------
d = djshell("import uuid\n"
            "from datetime import timedelta\n"
            "from django.utils import timezone\n"
            "from apps.tenants.context import tenant_context\n"
            "from apps.alerts.models import AlertInstance\n"
            "from apps.farms.models import Farm, Saloon\n"
            f"with tenant_context('{ORG}'):\n"
            "    f = Farm.objects.get(id=1); s = Saloon.objects.get(id=1)\n"
            "    a = AlertInstance.objects.create(id=uuid.uuid4(), org_id=f.org_id, farm=f, saloon=s,\n"
            "        metric_type='lux', current_status='ACTIVE_TIER_1', current_value=250,\n"
            "        source='REPLAY_ANALYSIS', batch_id='QA:replay',\n"
            "        triggered_at=timezone.now()-timedelta(minutes=40), highest_tier_reached=1)\n"
            "    print(a.id)")
rid12 = d.stdout.strip().splitlines()[-1]
sweep_org(); time.sleep(2); sweep_org()
st12 = psql(f"SELECT current_status FROM alert_instances WHERE id='{rid12}'").stdout.strip()
R.ok("9.12", st12 == "ACTIVE_TIER_1", f"REPLAY_ANALYSIS untouched by sweep: {st12}")
psql(f"DELETE FROM alert_instances WHERE id='{rid12}'")
left = [l for l in psql("SELECT id FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
R.ok("9.0pre", left == [], f"board clean for wave5c: {left}")
R.f.flush()
print("WAVE5B3 DONE", flush=True)
