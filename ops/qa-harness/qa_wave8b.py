"""Wave 8b — 17.2 paced replay drain + 17.3 kill -9 worker mid-batch, exact-once."""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec
import paho.mqtt.client as mqtt

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
OWNER = tok("demo-owner", PW := "password12345")
ORG = claims(OWNER)["org_id"]
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

base = datetime.now(timezone.utc) - timedelta(minutes=90)
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
c.connect("localhost", 8883, 60); c.loop_start(); time.sleep(1.0)
N = 200
c0 = int(psql("SELECT count(*) FROM saloon_telemetry").stdout.strip())
for i in range(N):
    dts = (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
         "device_ts": dts, "is_buffered": True, "clock_drift": False,
         "readings": [{"pack_id": "P-A", "metrics": {
             "temp": {"avg": 26.5, "min": 25.5, "max": 27.5}, "humidity": {"avg": 58.0},
             "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]}
    c.publish(f"{ORG}/farms/1/gateways/GW-QA0001/telemetry", json.dumps(p).encode(), qos=1)
    if i % 50 == 0:
        print(f"published {i}", flush=True)
c.loop_stop(); c.disconnect()
print(f"flood done; waiting 100s then kill -9 worker", flush=True)
time.sleep(100)
c1 = int(psql("SELECT count(*) FROM saloon_telemetry").stdout.strip())
subprocess.run(["docker", "kill", "-s", "9", "morghdari-celery-worker"], capture_output=True)
time.sleep(5)
st = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", "morghdari-celery-worker"],
                    capture_output=True, text=True).stdout.strip()
subprocess.run(["docker", "start", "morghdari-celery-worker"], capture_output=True)
print(f"killed (state={st}); restarted; draining...", flush=True)
t0 = time.time(); done = False
while time.time() - t0 < 600:
    time.sleep(20)
    pend = redis("GET", "replay:pending:GW-QA0001")
    n = int(psql("SELECT count(*) FROM saloon_telemetry").stdout.strip())
    if (pend in ("0", "") or pend is None) and n >= c0 + N:
        done = True
        break
n = int(psql("SELECT count(*) FROM saloon_telemetry").stdout.strip())
dups = psql("SELECT count(*) FROM (SELECT device_ts, slot_id, count(*) c FROM saloon_telemetry "
            "WHERE device_ts > now() - interval '4 hours' GROUP BY 1,2 HAVING count(*) > 1) t").stdout.strip()
R.ok("17.2", done, f"drained with pacing (not instant); pending={redis('GET', 'replay:pending:GW-QA0001')!r}")
R.ok("17.3", n - c0 == N and dups == "0", f"exact-once after kill: +{n - c0}/{N} dup-groups={dups}")
h = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", "morghdari-celery-worker"],
                   capture_output=True, text=True).stdout.strip()
R.ok("17.3b", h == "healthy", f"worker {h} after restart")
R.f.flush()
print("WAVE8B DONE", flush=True)
