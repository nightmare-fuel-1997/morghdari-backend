"""Wave 5c3 — section 10 redo from 10.4b. mute_id keys, DB-anchored windows."""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
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
MGR = tok("manager1", PW); WKR = tok("worker1", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(temp=None, nh3=None):
    metrics = {"temp": {"avg": 26.5, "min": 25.5, "max": 27.5}, "humidity": {"avg": 58.0},
               "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}
    if temp is not None:
        metrics["temp"] = {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)}
    if nh3 is not None:
        metrics["nh3"] = nh3
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": "P-A", "metrics": metrics}]}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/1/gateways/GW-QA0001/telemetry", json.dumps(p).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

def iso_plus(sec):
    return datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + sec, tz=timezone.utc).isoformat()

def idset(metric=None):
    q = f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND saloon_id=1"
    if metric: q += f" AND metric_type='{metric}'"
    return set(l.strip() for l in psql(q).stdout.strip().splitlines() if l.strip())

def wait_new(before, metric=None, timeout=150):
    t0 = time.time()
    while time.time() - t0 < timeout:
        diff = idset(metric) - before
        if diff: return diff
        time.sleep(5)
    return set()

def sweep_org():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import escalate_all\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('esc=', escalate_all())").stdout.strip()

def mutes():
    r = api("GET", "/maintenance-mutes/?active_only=false", token=MGR)
    return (r["json"] or {}).get("results", [])

def find_mute(reason):
    return next((m for m in mutes() if m.get("reason") == reason), None)

def cleanup():
    for m in mutes():
        api("DELETE", f"/maintenance-mutes/{m['mute_id']}/", token=MGR)
    for mk in ("nh3", "temp", "lux"):
        api("DELETE", f"/alert-subscriptions/{mk}/", token=MGR)

def active_ids():
    return set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND current_status LIKE 'ACTIVE%'"
    ).stdout.strip().splitlines() if l.strip())

def resolve_all():
    for _ in range(3):
        pub(); time.sleep(8)
    sweep_org(); time.sleep(3); sweep_org()

cleanup()
assert active_ids() == set(), "board not clean"
live = [m for m in mutes() if m.get("active")]
assert live == [], f"live mutes present: {live}"

# ---------- 10.4b already-running (wide margins) ----------
now = datetime.now(timezone.utc).isoformat()
r = api("POST", "/maintenance-mutes/", token=MGR,
        body={"saloon_id": 1, "starts_at": iso_plus(-600), "ends_at": iso_plus(3600), "reason": "qa running3"})
m = find_mute("qa running3")
R.ok("10.4b", r["status"] == 201 and m is not None and m.get("active") is True,
     {"status": r["status"], "listed_active": (m or {}).get("active")})
mid_r = m["mute_id"]
# ---------- 10.5 abutting: starts exactly when mid_r ends (DB-anchored) ----------
end_iso = m["ends_at"]
from datetime import datetime as _dt
end_dt = _dt.fromisoformat(end_iso)
abut_start = end_dt.isoformat()
abut_end = (end_dt + timedelta(seconds=100)).isoformat()
r = api("POST", "/maintenance-mutes/", token=MGR,
        body={"saloon_id": 1, "starts_at": abut_start, "ends_at": abut_end, "reason": "qa abut3"})
m2 = find_mute("qa abut3")
R.ok("10.5", r["status"] == 201 and m2 is not None, {"status": r["status"]})
# ---------- 10.6 overlap (inside mid_r) ----------
mid_ts = (end_dt - timedelta(seconds=1800)).isoformat()
r = api("POST", "/maintenance-mutes/", token=MGR,
        body={"saloon_id": 1, "starts_at": mid_ts, "ends_at": iso_plus(5400), "reason": "qa overlap3"})
R.ok("10.6", r["status"] == 409 and (r["json"] or {}).get("error") == "mute window overlaps an existing window for this saloon", r["json"])
# ---------- 10.7 revoke paths ----------
r = api("DELETE", f"/maintenance-mutes/{mid_r}/", token=MGR)
R.ok("10.7a", r["status"] == 200 and (r["json"] or {}).get("status") == "REVOKED", {"status": r["status"], "json": r["json"]})
r = api("DELETE", f"/maintenance-mutes/{mid_r}/", token=MGR)
R.ok("10.7b", r["status"] == 409 and (r["json"] or {}).get("error") == "mute is not active", r["json"])
r = api("DELETE", "/maintenance-mutes/999999/", token=MGR)
R.ok("10.7c", r["status"] == 404 and (r["json"] or {}).get("error") == "mute not found", r["json"])
if m2:
    api("DELETE", f"/maintenance-mutes/{m2['mute_id']}/", token=MGR)
# ---------- 10.8 role ----------
r = api("POST", "/maintenance-mutes/", token=WKR,
        body={"saloon_id": 1, "starts_at": now, "ends_at": iso_plus(3600), "reason": "qa x"})
R.ok("10.8", r["status"] == 403 and (r["json"] or {}).get("error") == "manager role required", r["json"])
# ---------- 10.9 window + digest (240s window, pubs in first 2 min) ----------
m = api("POST", "/maintenance-mutes/", token=MGR,
        body={"saloon_id": 1, "starts_at": datetime.now(timezone.utc).isoformat(), "ends_at": iso_plus(240), "reason": "qa digest3"})
m9 = find_mute("qa digest3")
assert m9, "mute not created"
mid9 = m9["mute_id"]
before = idset("temp")
for _ in range(6):
    pub(31.0); time.sleep(8)
time.sleep(150)
sweep_org(); time.sleep(3); sweep_org()
R.ok("10.9a", idset("temp") - before == set(), "suppressed during window")
dg = redis("EXISTS", f"maintenance:mute:digest:{mid9}")
cnt = redis("GET", f"maintenance:mute:count:{mid9}")
R.ok("10.9b", dg == "1", f"digest marker={dg} suppressed_count={cnt}")
api("DELETE", f"/maintenance-mutes/{mid9}/", token=MGR)
# ---------- 10.10 sub mute ----------
r = api("POST", "/alert-subscriptions/", token=MGR, body={"metric_key": "nh3", "reason": "qa"})
R.ok("10.10a", r["status"] in (200, 201), r["status"])
before = idset("nh3")
for _ in range(5):
    pub(None, nh3={"avg": 30.0, "min": 29.0, "max": 31.0, "max_ts": 0}); time.sleep(8)
time.sleep(40)
R.ok("10.10b", idset("nh3") - before == set(), "sub-muted category accumulates nothing")
import datetime as _dt2
day = (_dt2.datetime.now(_dt2.timezone.utc) + _dt2.timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d")
dg = redis("EXISTS", f"submuted:{ORG}:nh3:{day}")
R.ok("10.10c", dg == "1", f"sub-mute digest key present: {dg}")
r = api("POST", "/alert-subscriptions/", token=MGR, body={"metric_key": "nh3", "reason": "qa"})
R.ok("10.11a", r["status"] == 200 and (r["json"] or {}).get("idempotent") is True, r["json"])
r = api("DELETE", "/alert-subscriptions/humidity/", token=MGR)
R.ok("10.11b", r["status"] == 404 and (r["json"] or {}).get("error") == "metric not muted", r["json"])
r = api("POST", "/alert-subscriptions/", token=MGR, body={"metric_key": "xyz", "reason": "qa"})
R.ok("10.11c", r["status"] == 400 and (r["json"] or {}).get("error") == "unknown metric_key", r["json"])
before = idset("nh3")
pub(None, nh3={"avg": 59.0, "min": 58.0, "max": 60.0, "max_ts": 59})
R.ok("10.13", len(wait_new(before, "nh3", 120)) >= 1, "velocity exempt from subscription mute")
api("DELETE", "/alert-subscriptions/nh3/", token=MGR)
resolve_all()
ENG2 = tok("engineer1", PW)
r = api("GET", "/alerts/?status=BOGUS", token=ENG2)
R.ok("10.14a", r["status"] == 400 and (r["json"] or {}).get("error") == "status must be ACTIVE or ALL", r["json"])
r = api("GET", "/alerts/?tier=4", token=ENG2)
R.ok("10.14b", r["status"] == 400 and (r["json"] or {}).get("error") == "tier must be 1, 2 or 3", r["json"])
cleanup()
resolve_all()
left = [l for l in psql(f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND current_status LIKE 'ACTIVE%'").stdout.strip().splitlines() if l.strip()]
R.ok("10.0pre", left == [], f"board clean: {left}")
R.f.flush()
print("WAVE5C3 DONE", flush=True)
