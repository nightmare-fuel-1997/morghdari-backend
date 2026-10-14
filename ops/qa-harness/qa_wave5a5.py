"""Wave 5a5 — 8.11 THI ramp, 8.14 concurrent packs, 8.16 row shape."""
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
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(payload, farm=1):
    slot = payload["gateway_slot_id"]
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/{farm}/gateways/{slot}/telemetry", json.dumps(payload).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def M(temp=26.5, hum=58.0):
    return {"temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
            "humidity": {"avg": hum},
            "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}

def P(pack="P-A", hw="HWM-QA01", slot="GW-QA0001", metrics=None):
    return {"gateway_hardware_id": hw, "gateway_slot_id": slot, "schema_version": "2.0",
            "device_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
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

def status_of(aid):
    return psql(f"SELECT current_status FROM alert_instances WHERE id='{aid}'").stdout.strip()

def resolve_all():
    for _ in range(3):
        pub(P(metrics=M())); time.sleep(8)
    sweep_org(); time.sleep(3); sweep_org()

ar = [l for l in psql("SELECT id FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
R.ok("8.0pre", ar == [], f"active={ar}")
before = idset("thi", 1)
for tv, hu in [(28.0, 66.0), (30.0, 70.0), (32.0, 74.0)]:
    pub(P(metrics=M(temp=tv, hum=hu))); time.sleep(30)
for _ in range(4):
    pub(P(metrics=M(temp=35.0, hum=78.0))); time.sleep(8)
new = wait_new(before, "thi", 1, 120)
R.ok("8.11", len(new) >= 1, f"derived THI breached n={len(new)} (THI(35,78)=86.9>=79)")
resolve_all()
before = idset("temp", 1)
for _ in range(4):
    pub(P(pack="P-A", metrics=M(temp=31.0)))
    pub(P(pack="P-B", hw="HWM-QA01", slot="GW-QA0001", metrics=M(temp=31.0)))
    time.sleep(8)
new = wait_new(before, "temp", 1, 120)
R.ok("8.14", len(new) == 1, f"two packs breaching -> exactly one alert n={len(new)}")
resolve_all()
row = psql("SELECT current_status, source, highest_tier_reached FROM alert_instances ORDER BY triggered_at DESC LIMIT 1").stdout.strip()
parts = row.split("|")
R.ok("8.16", len(parts) == 3 and parts[1] in ("REALTIME", "REPLAY_ANALYSIS"), f"row={row}")
R.v("8.13", "NA", "THI-mute NameError path: code-reading only (evaluation.py:182-188); live repro deferred")
R.v("8.15", "NA", "hourly quota exhaustion unreachable on pro tier in test time (exempt flag config-verified)")
R.f.flush()
print("WAVE5A5 DONE", flush=True)
