"""Wave 5b1b — 9.1/9.2 redo with SUSTAINED breach feeds during sweep loops."""
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

def pub(temp, slot="GW-QA0001", hw="HWM-QA01", farm=1, pack="P-A"):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": hw, "gateway_slot_id": slot, "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": pack, "metrics": {
             "temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
             "humidity": {"avg": 58.0},
             "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/{farm}/gateways/{slot}/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def idset(saloon):
    return set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND saloon_id={saloon} AND metric_type='temp'"
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

# ---------- 9.1 ----------
before = idset(1)
for _ in range(6):
    pub(31.0); time.sleep(8)
t0 = time.time(); aid = None
while time.time() - t0 < 150:
    diff = idset(1) - before
    if diff:
        aid = sorted(diff)[0]; break
    time.sleep(5)
R.ok("9.1a", aid is not None and status_of(aid) == "ACTIVE_TIER_1", f"fresh T1 {aid[:8] if aid else None}")
seen = {status_of(aid)} if aid else set()
t0 = time.time()
while time.time() - t0 < 420 and aid and status_of(aid) != "ACTIVE_TIER_3":
    pub(31.0)  # keep the breach episode alive across the whole window
    sweep_org(); time.sleep(25)
    seen.add(status_of(aid))
R.ok("9.1b", "ACTIVE_TIER_2" in seen, f"ladder: {sorted(seen)}")
R.ok("9.1c", aid and status_of(aid) == "ACTIVE_TIER_3", f"final={status_of(aid) if aid else None}")

# ---------- 9.2 (saloon 2, ack at T1, sustain + sweep 5.5 min) ----------
before = idset(2)
for _ in range(6):
    pub(31.0, slot="GW-QA0002", hw="HWM-QA02", farm=2); time.sleep(8)
t0 = time.time(); aid2 = None
while time.time() - t0 < 150:
    diff = idset(2) - before
    if diff:
        aid2 = sorted(diff)[0]; break
    time.sleep(5)
assert aid2, "9.2 setup failed"
r = api("POST", f"/alerts/{aid2}/acknowledge/", token=tok("engineer1", PW))
assert r["status"] == 200, r
t0 = time.time(); hist = []
while time.time() - t0 < 330:
    pub(31.0, slot="GW-QA0002", hw="HWM-QA02", farm=2)
    sweep_org(); time.sleep(25)
    hist.append(status_of(aid2))
    if status_of(aid2) not in ("ACTIVE_TIER_1",):
        break
R.ok("9.2a", all(s == "ACTIVE_TIER_1" for s in hist), f"held T1 while paused: last3={hist[-3:]}")
R.ok("9.2b", status_of(aid2) in ("ACTIVE_TIER_2", "ACTIVE_TIER_3"), f"resumed after pause -> {status_of(aid2)}")
# resolve both
for _ in range(3):
    pub(26.5); time.sleep(6)
    pub(26.5, slot="GW-QA0002", hw="HWM-QA02", farm=2); time.sleep(6)
sweep_org(); time.sleep(3); sweep_org()
R.f.flush()
print("WAVE5B1B DONE", flush=True)
