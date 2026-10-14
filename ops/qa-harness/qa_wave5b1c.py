"""Wave 5b1c — 9.2 redo: pause=1min, fast sweep cadence (~22s), sustained feed."""
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
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub2(temp):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": "HWM-QA02", "gateway_slot_id": "GW-QA0002", "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": "P-A", "metrics": {
             "temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
             "humidity": {"avg": 58.0},
             "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/2/gateways/GW-QA0002/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def idset2():
    return set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND saloon_id=2 AND metric_type='temp'"
    ).stdout.strip().splitlines() if l.strip())

def status_of(aid):
    return psql(f"SELECT current_status FROM alert_instances WHERE id='{aid}'").stdout.strip()

def sweep_org():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import escalate_all\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('esc=', escalate_all())").stdout.strip()

psql("UPDATE system_configuration SET value='1' WHERE key='alert_acknowledgement_pause_minutes'")
ar = [l for l in psql("SELECT id FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
assert ar == [], f"board not clean: {ar}"

before = idset2()
for _ in range(6):
    pub2(31.0); time.sleep(8)
t0 = time.time(); aid2 = None
while time.time() - t0 < 150:
    diff = idset2() - before
    if diff:
        aid2 = sorted(diff)[0]; break
    time.sleep(5)
assert aid2, "9.2 setup failed"
r = api("POST", f"/alerts/{aid2}/acknowledge/", token=tok("engineer1", PW))
assert r["status"] == 200, r
t_ack = time.time()
R.ok("9.2pre", redis("TTL", f"alert:paused:{aid2}") not in ("-2", "-1", ""), f"pause key TTL={redis('TTL', f'alert:paused:{aid2}')}")
t0 = time.time(); hist = []
while time.time() - t0 < 240:
    pub2(31.0)
    sweep_org(); time.sleep(5)
    hist.append((round(time.time() - t_ack), status_of(aid2)))
    if status_of(aid2) not in ("ACTIVE_TIER_1",):
        break
frozen = [s for _, s in hist]
R.ok("9.2a", all(s == "ACTIVE_TIER_1" for s in frozen[:-1]) if len(hist) > 1 else False,
     f"held T1 while paused (tier2 mark at 60s passed): {hist}")
R.ok("9.2b", status_of(aid2) in ("ACTIVE_TIER_2", "ACTIVE_TIER_3"),
     f"resumed after pause expiry -> {status_of(aid2)} (full trace {hist})")
psql("UPDATE system_configuration SET value='3' WHERE key='alert_acknowledgement_pause_minutes'")
for _ in range(3):
    pub2(26.5); time.sleep(6)
sweep_org(); time.sleep(3); sweep_org()
R.f.flush()
print("WAVE5B1C DONE", flush=True)
