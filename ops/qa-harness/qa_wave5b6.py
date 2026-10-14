"""Wave 5b6 — 9.11 farm-wide correlation redo: exact shape capture."""
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

def pub(pack="P-A", hw="HWM-QA01", slot="GW-QA0001", farm=1):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": hw, "gateway_slot_id": slot, "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": pack, "metrics": {
             "temp": {"avg": 26.5, "min": 25.5, "max": 27.5}, "humidity": {"avg": 58.0},
             "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/{farm}/gateways/{slot}/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def los_ids():
    return set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND metric_type='los'"
    ).stdout.strip().splitlines() if l.strip())

def los_rows():
    return [l for l in psql(
        f"SELECT substr(id::text,1,8)||'|'||saloon_id||'|'||current_status||'|'||COALESCE(current_value::text,'?') FROM alert_instances "
        f"WHERE org_id='{ORG}' AND metric_type='los' ORDER BY triggered_at DESC LIMIT 8"
    ).stdout.strip().splitlines() if l.strip()]

def watch(n=3, gap=25):
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import los_watchdog\n"
                   f"with tenant_context('{ORG}'):\n"
                   f"    print([los_watchdog() for _ in range({n})])")

def clear_los_state():
    for pat in ("los:fired:*", "farm:corr:muted:*", "farm:los:*"):
        for k in [l for l in redis("KEYS", pat).splitlines() if l.strip()]:
            redis("DEL", k)

ar = [l for l in psql(f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
assert ar == [], f"board not clean: {ar}"
assert psql("SELECT value FROM system_configuration WHERE key='sensor_status_ttl_sec'").stdout.strip() == "120"
clear_los_state()

# all demo packs live (both saloons), verify
for _ in range(3):
    for pk in ("P-A", "P-B", "P-C"):
        pub(pk)
        pub(pk, hw="HWM-QA02", slot="GW-QA0002", farm=2)
    time.sleep(10)
# kill everything: no more pubs until fires observed
before = los_ids()
t0 = time.time(); fired = False
while time.time() - t0 < 420:
    watch(3, 25)
    time.sleep(10)
    if los_ids() - before:
        fired = True
        break
new = los_ids() - before
rows = los_rows()
corr = [l for l in redis("KEYS", "farm:corr:muted:*").splitlines() if l.strip()]
R.ok("9.11a", fired, f"farm-wide outage pages: new={len(new)} rows={rows}")
R.ok("9.11b", len(corr) >= 1, f"per-farm suppression marker set: {corr}")
# recover everything, resolve all
for _ in range(3):
    for pk in ("P-A", "P-B", "P-C"):
        pub(pk)
        pub(pk, hw="HWM-QA02", slot="GW-QA0002", farm=2)
    time.sleep(10)
t0 = time.time()
while time.time() - t0 < 240:
    watch(3, 25)
    left = [l for l in los_rows() if "ACTIVE" in l]
    if not left:
        break
    time.sleep(10)
left = [l for l in los_rows() if "ACTIVE" in l]
R.ok("9.11c", left == [], f"all recovered+resolved: {left}")
R.f.flush()
print("WAVE5B6 DONE", flush=True)
