"""Wave 5c5 — 10.9b redo: short window, sweep AFTER end while count key alive."""
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
MGR = tok("manager1", PW)
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

def sweep_org():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import escalate_all\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('esc=', escalate_all())").stdout.strip()

def idset(metric=None):
    q = f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND saloon_id=1"
    if metric: q += f" AND metric_type='{metric}'"
    return set(l.strip() for l in psql(q).stdout.strip().splitlines() if l.strip())

now = datetime.now(timezone.utc).isoformat()
end = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 100, tz=timezone.utc).isoformat()
m = api("POST", "/maintenance-mutes/", token=MGR,
        body={"saloon_id": 1, "starts_at": now, "ends_at": end, "reason": "qa digest5"})
assert m["status"] == 201, m
mid9 = next(x for x in ((api("GET", "/maintenance-mutes/?active_only=false", token=MGR)["json"] or {}).get("results", [])) if x.get("reason") == "qa digest5")["mute_id"]
before = idset("temp")
for _ in range(4):
    pub(31.0); time.sleep(8)
# window is 100s; pubs span ~36s; wait until 130s (past end, count key alive till ~160s)
time.sleep(95)
sweep_org(); time.sleep(3); sweep_org()
R.ok("10.9a", idset("temp") - before == set(), "suppressed during window")
dg = redis("EXISTS", f"maintenance:mute:digest:{mid9}")
cnt = redis("GET", f"maintenance:mute:count:{mid9}")
R.ok("10.9b", dg == "1", f"digest marker={dg} (count was consumed: {cnt!r})")
api("DELETE", f"/maintenance-mutes/{mid9}/", token=MGR)
for _ in range(2):
    pub(26.5); time.sleep(8)
sweep_org()
R.f.flush()
print("WAVE5C5 DONE", flush=True)
