"""Wave 5b5 — 9.9 redo: TTL=120, precondition polls, keep-alive cadence fits TTL."""
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
SS = {"P-A": "SS-00010", "P-B": "SS-00011", "P-C": "SS-00012"}
SS2 = {"P-A": "SS-00020", "P-B": "SS-00021", "P-C": "SS-00022"}

def pub(pack="P-A", temp=26.5):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": pack, "metrics": {
             "temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
             "humidity": {"avg": 58.0},
             "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/1/gateways/GW-QA0001/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def pub2(pack="P-A", temp=26.5):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": "HWM-QA02", "gateway_slot_id": "GW-QA0002", "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": pack, "metrics": {
             "temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
             "humidity": {"avg": 58.0},
             "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/2/gateways/GW-QA0002/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def key_present2(pack):
    return redis("EXISTS", f"sensor:status:2:{SS2[pack]}") == "1"

def wait_key2(pack, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if key_present2(pack):
            return True
        time.sleep(5)
    return False

def los_ids():
    return set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND metric_type='los'"
    ).stdout.strip().splitlines() if l.strip())

def los_rows():
    return [l for l in psql(
        f"SELECT substr(id::text,1,8)||'|'||saloon_id||'|'||current_status FROM alert_instances "
        f"WHERE org_id='{ORG}' AND metric_type='los' ORDER BY triggered_at DESC LIMIT 6"
    ).stdout.strip().splitlines() if l.strip()]

def watch():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import los_watchdog\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('los=', los_watchdog())").stdout.strip()

def key_present(pack):
    return redis("EXISTS", f"sensor:status:1:{SS[pack]}") == "1"

def wait_key(pack, present=True, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if key_present(pack) == present:
            return True
        time.sleep(5)
    return False

def clear_los_state():
    for pat in ("los:fired:*", "farm:corr:muted:*", "farm:los:*"):
        for k in [l for l in redis("KEYS", pat).splitlines() if l.strip()]:
            redis("DEL", k)

ar = [l for l in psql(f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
assert ar == [], f"board not clean: {ar}"
psql("UPDATE system_configuration SET value='120' WHERE key='sensor_status_ttl_sec'")
assert psql("SELECT value FROM system_configuration WHERE key='sensor_status_ttl_sec'").stdout.strip() == "120"
clear_los_state()

# seed ALL demo packs live (saloon 1 + saloon 2; nobody never-live), verify keys
for _ in range(3):
    for pk in ("P-A", "P-B", "P-C"):
        pub(pk)
        pub2(pk)
    time.sleep(10)
R.ok("9.9pre", all(wait_key(pk, True, 60) for pk in ("P-A", "P-B", "P-C")) and
     all(wait_key2(pk, 60) for pk in ("P-A", "P-B", "P-C")), "all 6 demo slots live")
before = los_ids()
# kill P-A (saloon 1) only; keep the other five alive
t0 = time.time()
while time.time() - t0 < 300:
    for pk in ("P-B", "P-C"):
        pub(pk)
    for pk in ("P-B", "P-C"):
        pub2(pk)
    time.sleep(15)
    watch()
    time.sleep(15)
    if los_ids() - before:
        break
new = los_ids() - before
rows = los_rows()
R.ok("9.9a", len(new) == 1, f"exactly one LOS; rows={rows}")
# recovery: P-A live -> fired cleared + row resolved (polled, not assumed)
for _ in range(3):
    pub("P-A"); time.sleep(10)
t0 = time.time(); rec = False
while time.time() - t0 < 150:
    watch()
    if redis("EXISTS", "los:fired:1:SS-00010") == "0":
        rec = True; break
    time.sleep(15)
R.ok("9.9b1", rec, "recovery heartbeat cleared the fired key")
# resolve the row too (watchdog resolves on seen-live)
t0 = time.time(); res = False
while time.time() - t0 < 150:
    watch()
    r = [l for l in los_rows() if "ACTIVE" in l]
    if not [l for l in r if l.split("|")[1] == "1"]:
        res = True; break
    time.sleep(15)
R.ok("9.9b2", res, "recovered row RESOLVED, episode fully closed")
# second episode for P-A
before2 = los_ids()
t0 = time.time()
while time.time() - t0 < 300:
    for pk in ("P-B", "P-C"):
        pub(pk)
    time.sleep(15)
    watch()
    time.sleep(15)
    if los_ids() - before2:
        break
R.ok("9.9c", len(los_ids() - before2) == 1, "second episode re-fires exactly once")
# resolve everything for the next wave
for _ in range(3):
    for pk in ("P-A", "P-B", "P-C"):
        pub(pk)
    time.sleep(10)
t0 = time.time()
while time.time() - t0 < 180:
    watch()
    if not [l for l in los_rows() if "ACTIVE" in l]:
        break
    time.sleep(15)
left = [l for l in los_rows() if "ACTIVE" in l]
R.ok("9.9d", left == [], f"all los resolved: {left}")
R.f.flush()
print("WAVE5B5 DONE", flush=True)
