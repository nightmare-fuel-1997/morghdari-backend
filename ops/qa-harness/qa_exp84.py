"""Controlled resolve experiment: fire -> normal -> per-org sweep, all outputs shown."""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
env1 = {}
for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8"):
    m = re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())
    if m:
        env1[m.group(1)] = m.group(2)

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()

ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])
def djshell(py):
    r = subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                        "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                       capture_output=True, text=True)
    print("SHELL rc=", r.returncode, "stdout=", r.stdout.strip().splitlines()[-2:] if r.stdout.strip() else [], "stderr=", r.stderr.strip()[-200:] if r.stderr.strip() else "", flush=True)
    return r

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

OWNER = tok("demo-owner", "password12345")
ORG = claims(OWNER)["org_id"]
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(metrics):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": "P-A", "metrics": metrics}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    info = c.publish(f"{ORG}/farms/1/gateways/GW-QA0001/telemetry", json.dumps(p).encode(), qos=1)
    try:
        info.wait_for_publish(timeout=8); print("pub acked", dts, flush=True)
    except Exception as e:
        print("PUB FAILED", e, flush=True)
    time.sleep(0.4); c.loop_stop(); c.disconnect()
    return dts

def M(temp=26.5):
    return {"temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
            "humidity": {"avg": 58.0},
            "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}

def rows():
    return [l for l in psql(f"SELECT substr(id::text,1,8)||'|'||metric_type||'|'||current_status FROM alert_instances WHERE org_id='{ORG}' ORDER BY triggered_at DESC").splitlines() if l.strip()]

print(" Tunables:", psql("SELECT value FROM system_configuration WHERE key='breach_duration_sec'"), flush=True)
before = set(rows())
print("rows before:", before, flush=True)
for _ in range(5):
    pub(M(30.0)); time.sleep(8)
time.sleep(20)
after = set(rows()) - before
print("new rows after feed:", after, flush=True)
print("breach keys:", redis("KEYS", "saloon:1:breach:*"), flush=True)
if after:
    aid = sorted(after)[0].split("|")[0]
    for _ in range(3):
        pub(M(26.5)); time.sleep(8)
    print("breach keys after normal:", redis("KEYS", "saloon:1:breach:*"), flush=True)
    djshell("from apps.tenants.context import tenant_context;"
            "from apps.alerts.services.sweeps import escalate_all;"
            f"with tenant_context('{ORG}'): print('esc=', escalate_all())")
    print("status now:", psql(f"SELECT current_status FROM alert_instances WHERE id LIKE '{aid}%'"), flush=True)
print("EXP DONE", flush=True)
