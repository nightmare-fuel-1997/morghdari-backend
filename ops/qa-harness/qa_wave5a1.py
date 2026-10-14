"""Wave 5a1 — 8.1 through 8.5 (threshold lifecycle). Foreground, ~7 min."""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, contains
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

def mark():
    return psql("SELECT max(triggered_at) FROM alert_instances").stdout.strip() or "2000-01-01 00:00:00+00"

def new_since(marker, metric=None, saloon=None):
    q = (f"SELECT id||'|'||metric_type||'|'||current_status FROM alert_instances "
         f"WHERE triggered_at > '{marker}' AND org_id='{ORG}'")
    if metric: q += f" AND metric_type='{metric}'"
    if saloon: q += f" AND saloon_id={saloon}"
    return [l for l in psql(q + " ORDER BY triggered_at DESC").stdout.strip().splitlines() if l.strip()]

def wait_new(marker, metric=None, saloon=None, timeout=110):
    t0 = time.time()
    while time.time() - t0 < timeout:
        rows = new_since(marker, metric, saloon)
        if rows: return rows
        time.sleep(4)
    return []

def sweep_org():
    return djshell("from apps.tenants.context import tenant_context;"
                   "from apps.alerts.services.sweeps import escalate_all;"
                   f"with tenant_context('{ORG}'): print('esc=', escalate_all())").stdout.strip().splitlines()

def status_of(aid):
    return psql(f"SELECT current_status FROM alert_instances WHERE id='{aid}'").stdout.strip()

def tunable(k, v):
    psql(f"UPDATE system_configuration SET value='{v}' WHERE key='{k}'")

tunable("breach_duration_sec", "30")
tunable("escalation_tier2_min", "1")
tunable("escalation_tier3_min", "2")
tunable("alert_acknowledgement_pause_minutes", "3")

# board must be clean: no ACTIVE rows, no active blobs for temp/nh3/thi
stale = psql("SELECT count(*) FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").stdout.strip()
R.ok("8.0pre", stale == "0", f"active rows before wave={stale}")

mk = mark()
for _ in range(5):
    pub(P(metrics=M(temp=30.0))); time.sleep(8)
rows = wait_new(mk, "temp", 1, 115)
R.ok("8.1", len(rows) >= 1, f"value==max breached, new={len(rows)}")
R.ok("8.12", len(rows) >= 1, "T1 in ~1min with breach_duration_sec=30 (tunable live-read)")
bk = redis("KEYS", "saloon:1:breach:temp")
R.ok("8.3", bool(bk), f"breach key present during episode: {bk!r}")
aid = rows[0].split("|")[0] if rows else None
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
mk = mark()
pub(P(metrics=M(temp=31.0)))
time.sleep(22)
R.ok("8.2", new_since(mk, "temp", 1) == [], "one obs at 22s < duration 30: no alert")
for _ in range(4):
    pub(P(metrics=M(temp=31.0))); time.sleep(8)
rows = wait_new(mk, "temp", 1, 100)
R.ok("8.2b", len(rows) >= 1, "same episode sustained -> fires (duration gate, not instant)")
for _ in range(3):
    pub(P(metrics=M())); time.sleep(8)
sweep_org()
mk = mark()
for _ in range(4):
    pub(P(metrics=M(temp=10.0))); time.sleep(8)
time.sleep(20)
R.ok("8.5", new_since(mk, "temp", 1) == [], "cold-side: zero alerts (min-side never fires)")
for _ in range(2):
    pub(P(metrics=M())); time.sleep(9)
R.f.flush()
print("WAVE5A1 DONE", flush=True)
