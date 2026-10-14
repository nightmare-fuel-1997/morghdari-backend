"""Wave 5b7 — 9.10a/b redo: verify status keys exist BEFORE kill (precondition poll)."""
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

def watch(n=3, gap=25):
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import los_watchdog\n"
                   f"with tenant_context('{ORG}'):\n"
                   f"    print([los_watchdog() for _ in range({n})])")

def clear_los_state():
    for pat in ("los:fired:*", "farm:corr:muted:*", "farm:los:*"):
        for k in [l for l in redis("KEYS", pat).splitlines() if l.strip()]:
            redis("DEL", k)

def key_present():
    return redis("EXISTS", "sensor:status:1:SS-00010") == "1"

def wait_key(present=True, timeout=150):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if key_present() == present:
            return True
        time.sleep(5)
    return False

def heartbeat(n=4):
    for _ in range(n):
        pub()
        time.sleep(9)

def active_los():
    return [l for l in psql(
        f"SELECT substr(id::text,1,8) FROM alert_instances WHERE org_id='{ORG}' AND metric_type='los' AND current_status LIKE 'ACTIVE%'"
    ).stdout.strip().splitlines() if l.strip()]

ar = active_los()
assert ar == [], f"board not clean: {ar}"

# ---------- 9.10a: suppress=10, restart, heartbeat (verified live), kill, poll ----------
assert psql("SELECT value FROM system_configuration WHERE key='los_suppress_after_redis_restart_min'").stdout.strip() == "10"
subprocess.run(["docker", "restart", "morghdari-redis"], capture_output=True)
time.sleep(25)
djshell("from apps.alerts.services.thresholds import recache_all_saloons\nprint(recache_all_saloons())")
up = int(redis("INFO", "server").split("uptime_in_seconds:")[1].split()[0])
clear_los_state()
heartbeat()
R.ok("9.10pre", wait_key(True, 120), "status key live after restart+heartbeat")
before = los_ids()
t0 = time.time()
while time.time() - t0 < 200:
    watch(2, 20)
    time.sleep(15)
    if los_ids() - before:
        break
R.ok("9.10a", los_ids() - before == set(), f"no LOS within 10min of restart (uptime was {up}s)")
heartbeat()
time.sleep(5)

# ---------- 9.10b: suppress=1, restart, same with verified keys ----------
psql("UPDATE system_configuration SET value='1' WHERE key='los_suppress_after_redis_restart_min'")
assert psql("SELECT value FROM system_configuration WHERE key='los_suppress_after_redis_restart_min'").stdout.strip() == "1"
subprocess.run(["docker", "restart", "morghdari-redis"], capture_output=True)
time.sleep(25)
djshell("from apps.alerts.services.thresholds import recache_all_saloons\nprint(recache_all_saloons())")
clear_los_state()
heartbeat()
R.ok("9.10pre", wait_key(True, 120), "status key live after 2nd restart+heartbeat")
subprocess.run([sys.executable, os.path.join(HERE, "qa_hb.py")], capture_output=True, timeout=300)
before = los_ids()
t0 = time.time()
while time.time() - t0 < 240:
    watch(2, 20)
    time.sleep(15)
    if los_ids() - before:
        break
new = los_ids() - before
R.ok("9.10b", len(new) >= 1, f"LOS fires once restart-suppression expired; n={len(new)}")
heartbeat()
psql("UPDATE system_configuration SET value='10' WHERE key='los_suppress_after_redis_restart_min'")
psql("UPDATE system_configuration SET value='300' WHERE key='sensor_status_ttl_sec'")
R.f.flush()
print("WAVE5B7 DONE", flush=True)
