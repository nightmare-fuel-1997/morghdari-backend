"""Wave 7b — 20.2 completion: ack the prod T1, sweep to T2, resolve."""
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

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
ORG = OW["org_id"]
ENG = tok("engineer1", PW)
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

def status_of(aid):
    return psql(f"SELECT current_status FROM alert_instances WHERE id='{aid}'").stdout.strip()

def sweep_org():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import escalate_all\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('esc=', escalate_all())").stdout.strip()

aid = "d648eb61-0000-0000-0000-000000000000"
aid = psql("SELECT id FROM alert_instances WHERE metric_type='temp' AND saloon_id=1 AND current_status='ACTIVE_TIER_1' ORDER BY triggered_at DESC LIMIT 1").stdout.strip()
R.ok("20.2a", bool(aid) and status_of(aid) == "ACTIVE_TIER_1", f"prod T1 alive: {aid[:8]}")
r = api("POST", f"/alerts/{aid}/acknowledge/", token=ENG)
R.ok("20.2b", r["status"] == 200, "acked at prod scale")
t0 = time.time()
while time.time() - t0 < 1500:
    pub(31.0)
    sweep_org()
    time.sleep(40)
    if status_of(aid) == "ACTIVE_TIER_2":
        break
R.ok("20.2c", status_of(aid) == "ACTIVE_TIER_2", f"escalated to T2 at prod tier2=15min: {status_of(aid)}")
for _ in range(4):
    pub(26.5); time.sleep(10)
t0 = time.time()
while time.time() - t0 < 300:
    sweep_org()
    time.sleep(20)
    if status_of(aid) == "RESOLVED":
        break
R.ok("20.2d", status_of(aid) == "RESOLVED", f"value normal -> {status_of(aid)}")
R.f.flush()
print("WAVE7B DONE", flush=True)
