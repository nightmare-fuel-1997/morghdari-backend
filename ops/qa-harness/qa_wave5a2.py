"""Wave 5a2 — 8.1..8.5 threshold lifecycle. ID-set oracles (clock-immune)."""
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
    return subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                           "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                          capture_output=True, text=True)

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
ORG = OW["org_id"]
MGR = tok("manager1", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(payload, farm=1):
    slot = payload["gateway_slot_id"]
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/{farm}/gateways/{slot}/telemetry", json.dumps(payload).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def M(temp=26.5, nh3=12.0, hum=58.0, nh3_ts=0):
    return {"temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
            "humidity": {"avg": hum},
            "nh3": {"avg": nh3, "min": max(0.0, nh3 - 1.0), "max": nh3 + 1.0, "max_ts": nh3_ts}}

def P(pack="P-A", hw="HWM-QA01", slot="GW-QA0001", metrics=None, dts=None):
    return {"gateway_hardware_id": hw, "gateway_slot_id": slot, "schema_version": "2.0",
            "device_ts": dts or iso(), "is_buffered": False, "clock_drift": False,
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
    r = djshell("from apps.tenants.context import tenant_context\n"
                "from apps.alerts.services.sweeps import escalate_all\n"
                f"with tenant_context('{ORG}'):\n"
                "    print('esc=', escalate_all())")
    assert r.returncode == 0, "sweep shell failed: " + (r.stderr or "")[-300:]
    return r.stdout.strip().splitlines()

def status_of(aid):
    return psql(f"SELECT current_status FROM alert_instances WHERE id='{aid}'").stdout.strip()

def tunable(k, v):
    psql(f"UPDATE system_configuration SET value='{v}' WHERE key='{k}'")

tunable("breach_duration_sec", "30")
tunable("escalation_tier2_min", "1")
tunable("escalation_tier3_min", "2")
tunable("alert_acknowledgement_pause_minutes", "3")
stale = [i for i in psql("SELECT id FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if i.strip()]
R.ok("8.0pre", stale == [], f"active rows before wave={len(stale)}")

# ---------- 8.1 boundary == max ----------
before = idset("temp", 1)
for _ in range(5):
    pub(P(metrics=M(temp=30.0))); time.sleep(8)
new = wait_new(before, "temp", 1, 180)
R.ok("8.1", len(new) >= 1, f"value==max breached, new={len(new)}")
R.ok("8.12", len(new) >= 1, "T1 with breach_duration_sec=30 (tunable live-read)")
bk = redis("KEYS", "saloon:1:breach:temp")
R.ok("8.3", bool(bk), f"breach key present during episode: {bk!r}")
aid = next(iter(new)) if new else None
if aid:
    pub(P(metrics=M(temp=29.7))); time.sleep(8)
    pub(P(metrics=M(temp=29.7))); time.sleep(8)
    R.ok("8.4a", status_of(aid).startswith("ACTIVE"), f"29.7 (max-0.3) still {status_of(aid)}")
    for _ in range(3):
        pub(P(metrics=M())); time.sleep(8)
    sweep_org(); time.sleep(3); sweep_org()
    R.ok("8.4b", status_of(aid) == "RESOLVED", f"sustained normal + sweep -> {status_of(aid)}")
else:
    R.v("8.4a", "NA", "no alert to track"); R.v("8.4b", "NA", "no alert to track")
# ---------- 8.2 single obs never fires; sustained does ----------
before = idset("temp", 1)
pub(P(metrics=M(temp=31.0)))
time.sleep(22)
R.ok("8.2", idset("temp", 1) - before == set(), "one obs < duration: no alert")
for _ in range(4):
    pub(P(metrics=M(temp=31.0))); time.sleep(8)
new = wait_new(before, "temp", 1, 150)
R.ok("8.2b", len(new) >= 1, "same episode sustained -> fires")
for _ in range(3):
    pub(P(metrics=M())); time.sleep(8)
sweep_org()
# ---------- 8.5 min-side ----------
before = idset("temp", 1)
for _ in range(4):
    pub(P(metrics=M(temp=10.0))); time.sleep(8)
time.sleep(20)
R.ok("8.5", idset("temp", 1) - before == set(), "cold-side: zero alerts")
for _ in range(2):
    pub(P(metrics=M())); time.sleep(9)
R.f.flush()
print("WAVE5A2 DONE", flush=True)
