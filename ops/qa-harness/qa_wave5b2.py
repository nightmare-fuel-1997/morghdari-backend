"""Wave 5b2 — 9.3/9.4/9.5/9.6 ack matrix, 9.7 resolve, 9.8 velocity-during-ack, 9.12 replay exclusion."""
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
WKR = tok("worker1", PW); ENG = tok("engineer1", PW); MGR = tok("manager1", PW)
DOC = tok("doctor1", PW); OTHER = tok("other-owner", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(temp, nh3=None):
    metrics = {"temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
               "humidity": {"avg": 58.0},
               "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}
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

def idset():
    return set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND saloon_id=1 AND metric_type='temp'"
    ).stdout.strip().splitlines() if l.strip())

def status_of(aid):
    return psql(f"SELECT current_status FROM alert_instances WHERE id='{aid}'").stdout.strip()

def sweep_org():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import escalate_all\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('esc=', escalate_all())").stdout.strip()

ar = [l for l in psql("SELECT id FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
assert ar == [], f"board not clean: {ar}"

before = idset()
for _ in range(6):
    pub(31.0); time.sleep(8)
t0 = time.time(); aid = None
while time.time() - t0 < 150:
    diff = idset() - before
    if diff:
        aid = sorted(diff)[0]; break
    time.sleep(5)
assert aid, "setup failed: no alert"
R.ok("9.3a", api("POST", f"/alerts/{aid}/acknowledge/", token=DOC)["status"] == 403, "doctor denied")
r = api("POST", f"/alerts/{aid}/acknowledge/", token=DOC)
R.ok("9.3b", (r["json"] or {}).get("error") == "assigned worker/engineer required", r["json"])
for name, t in [("worker", WKR), ("engineer", ENG), ("manager", MGR), ("owner", OWNER)]:
    r = api("POST", f"/alerts/{aid}/acknowledge/", token=t)
    R.ok(f"9.3-{name}", r["status"] == 200 and (r["json"] or {}).get("status") == "ACKNOWLEDGED",
         {"role": name, "status": r["status"]})
r = api("POST", "/alerts/00000000-0000-0000-0000-000000000000/acknowledge/", token=ENG)
R.ok("9.4a", r["status"] == 404 and (r["json"] or {}).get("error") == "alert not found", r["json"])
r = api("POST", f"/alerts/{aid}/acknowledge/", token=OTHER)
R.ok("9.4b", r["status"] == 404 and (r["json"] or {}).get("error") == "alert not found", "cross-org invisible")
n = int(psql(f"SELECT count(*) FROM alert_acknowledgements WHERE alert_id='{aid}'").stdout.strip())
R.ok("9.5", n >= 4, f"duplicate acks allowed, rows={n} (each refreshes pause)")
R.ok("9.6", status_of(aid).startswith("ACTIVE"), f"ack changes nothing on the alert itself: {status_of(aid)}")
# 9.7 resolve via normals + sweep
for _ in range(3):
    pub(26.5); time.sleep(8)
sweep_org(); time.sleep(3); sweep_org()
st = status_of(aid)
blob = redis("GET", "alert:active:saloon:1:temp")
R.ok("9.7", st == "RESOLVED" and not blob, f"status={st} blob_gone={not blob}")
# 9.8 velocity during an ACTIVE ack pause: fresh temp alert, ack it, spike NH3
before = idset()
for _ in range(6):
    pub(31.0); time.sleep(8)
t0 = time.time(); aid8 = None
while time.time() - t0 < 150:
    diff = idset() - before
    if diff:
        aid8 = sorted(diff)[0]; break
    time.sleep(5)
assert aid8, "9.8 setup failed"
assert api("POST", f"/alerts/{aid8}/acknowledge/", token=ENG)["status"] == 200
before_nh = set(l.strip() for l in psql(
    f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND saloon_id=1 AND metric_type='nh3'"
).stdout.strip().splitlines() if l.strip())
pub(26.5, nh3={"avg": 59.0, "min": 58.0, "max": 60.0, "max_ts": 59})
t0 = time.time(); new = set()
while time.time() - t0 < 100:
    cur = set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND saloon_id=1 AND metric_type='nh3'"
    ).stdout.strip().splitlines() if l.strip())
    if cur - before_nh:
        new = cur - before_nh; break
    time.sleep(5)
R.ok("9.8", len(new) >= 1, "velocity pages despite ack pause elsewhere")
for _ in range(3):
    pub(26.5); time.sleep(8)
sweep_org(); time.sleep(3); sweep_org()
# 9.12 fabricated old REPLAY_ANALYSIS alert must not escalate
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
R.f.flush()
print("WAVE5B2 DONE", flush=True)
