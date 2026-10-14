"""Wave 5b4 — 9.9 LOS single, 9.11 correlation, 9.10 redis-restart suppression."""
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

def pub(pack="P-A", hw="HWM-QA01", slot="GW-QA0001", farm=1, temp=26.5):
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

def los_ids():
    return set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND metric_type='los'"
    ).stdout.strip().splitlines() if l.strip())

def watch_org(rounds=6, gap=30):
    for _ in range(rounds):
        djshell("from apps.tenants.context import tenant_context\n"
                "from apps.alerts.services.sweeps import los_watchdog\n"
                f"with tenant_context('{ORG}'):\n"
                "    print('los=', los_watchdog())")
        time.sleep(gap)

def heartbeat(packs=("P-A",), n=4, gap=9):
    for _ in range(n):
        for pk in packs:
            pub(pack=pk)
        time.sleep(gap)

def active_los():
    return [l for l in psql(
        f"SELECT substr(id::text,1,8)||'|'||current_status FROM alert_instances "
        f"WHERE org_id='{ORG}' AND metric_type='los' AND current_status LIKE 'ACTIVE%'"
    ).stdout.strip().splitlines() if l.strip()]

def clear_los_state():
    for k in [l for l in redis("KEYS", "los:fired:*").splitlines() if l.strip()]:
        redis("DEL", k)
    for k in [l for l in redis("KEYS", "farm:corr:muted:*").splitlines() if l.strip()]:
        redis("DEL", k)
    for k in [l for l in redis("KEYS", "farm:los:*").splitlines() if l.strip()]:
        redis("DEL", k)

ar = [l for l in psql(f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
assert ar == [], f"board not clean: {ar}"
psql("UPDATE system_configuration SET value='30' WHERE key='sensor_status_ttl_sec'")

# ---------- 9.9 single-slot LOS + recovery re-arm ----------
# Keep P-B/C alive throughout so only P-A expires (1/3 < 0.7: no correlation).
clear_los_state()
heartbeat(("P-A", "P-B", "P-C"), n=2)
before = los_ids()
t0 = time.time()
while time.time() - t0 < 240:
    for pk in ("P-B", "P-C"):
        pub(pack=pk)
    time.sleep(20)
    watch_org(rounds=1, gap=1)
    if los_ids() - before:
        break
    time.sleep(5)
new = los_ids() - before
R.ok("9.9a", len(new) == 1, f"exactly one LOS for the expired slot; rows={sorted(new)}")
aid9 = sorted(new)[0] if new else None
# recovery: live again -> fired key cleared -> kill P-A again -> second episode
heartbeat(("P-A",), n=2)
time.sleep(5)
fired_gone = redis("EXISTS", "los:fired:1:SS-00010") == "0"
before2 = los_ids()
t0 = time.time()
while time.time() - t0 < 240:
    for pk in ("P-B", "P-C"):
        pub(pack=pk)
    time.sleep(20)
    watch_org(rounds=1, gap=1)
    if los_ids() - before2:
        break
    time.sleep(5)
R.ok("9.9b", fired_gone and len(los_ids() - before2) == 1, f"recovery cleared fired-key={fired_gone}; 2nd episode fired={len(los_ids()-before2)}")
for _ in range(2):
    pub(); time.sleep(8)

# ---------- 9.11 farm-wide correlation ----------
clear_los_state()
before = los_ids()
heartbeat(("P-A", "P-B", "P-C"), n=3)
t0 = time.time()
while time.time() - t0 < 240:
    watch_org(rounds=1, gap=1)
    if len(los_ids() - before) >= 1:
        break
    time.sleep(20)
new = los_ids() - before
rows = psql(f"SELECT metric_type||'|'||current_status FROM alert_instances WHERE org_id='{ORG}' AND metric_type='los' AND triggered_at > now() - interval '10 minutes'").stdout.strip()
R.ok("9.11", len(new) >= 1, f"correlation path produced {len(new)} los-family row(s); recent={rows[:200]!r}")
for _ in range(2):
    pub(); time.sleep(8)

# ---------- 9.10a restart suppresses (default 10min) ----------
subprocess.run(["docker", "restart", "morghdari-redis"], capture_output=True)
time.sleep(25)
djshell("from apps.alerts.services.thresholds import recache_all_saloons\nprint(recache_all_saloons())")
up = int(redis("INFO", "server").split("uptime_in_seconds:")[1].split()[0])
clear_los_state()
before = los_ids()
heartbeat(("P-A",), n=3)
t0 = time.time()
while time.time() - t0 < 170:
    watch_org(rounds=1, gap=1)
    time.sleep(20)
R.ok("9.10a", los_ids() - before == set(), f"no LOS within 10min of redis restart (uptime was {up}s)")
for _ in range(2):
    pub(); time.sleep(8)
# ---------- 9.10b suppression expired (set 1min) ----------
psql("UPDATE system_configuration SET value='1' WHERE key='los_suppress_after_redis_restart_min'")
subprocess.run(["docker", "restart", "morghdari-redis"], capture_output=True)
time.sleep(25)
djshell("from apps.alerts.services.thresholds import recache_all_saloons\nprint(recache_all_saloons())")
for k in [l for l in redis("KEYS", "farm:corr:muted:*").splitlines() if l.strip()]:
    redis("DEL", k)
clear_los_state()
before = los_ids()
heartbeat(("P-A",), n=3)
t0 = time.time()
while time.time() - t0 < 200:
    watch_org(rounds=1, gap=1)
    if los_ids() - before:
        break
    time.sleep(20)
R.ok("9.10b", len(los_ids() - before) >= 1, "LOS fires once restart-suppression expired")
for _ in range(2):
    pub(); time.sleep(8)
psql("UPDATE system_configuration SET value='10' WHERE key='los_suppress_after_redis_restart_min'")
psql("UPDATE system_configuration SET value='300' WHERE key='sensor_status_ttl_sec'")
psql("UPDATE system_configuration SET value='900' WHERE key='breach_duration_sec'")
psql("UPDATE system_configuration SET value='15' WHERE key='escalation_tier2_min'")
psql("UPDATE system_configuration SET value='30' WHERE key='escalation_tier3_min'")
psql("UPDATE system_configuration SET value='10' WHERE key='alert_acknowledgement_pause_minutes'")
djshell("from apps.alerts.services.thresholds import recache_all_saloons\nprint(recache_all_saloons())")
R.f.flush()
print("WAVE5B4 DONE", flush=True)
