"""Wave 9b — section 22 robustness (22.2-22.9, 22.11-22.13, 22.15). 22.1/22.10 done in 9a; 22.14 rides 19.6."""
import base64
import hashlib
import hmac
import os
import re
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, contains

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
RUN = str(int(time.time()))[-6:]
MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])
def djshell(py, inp=None):
    cmd = (["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
            "morghdari-daphne", "python", "manage.py", "shell", "-c", py] if inp is None else
           ["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
            "-e", "REDIS_URL=redis://:%s@redis:6379" % env1["REDIS_PASSWORD"],
            "morghdari-daphne", "python", "manage.py", "shell"])
    r = subprocess.run(cmd, capture_output=True, text=True, input=inp)
    assert r.returncode == 0, "shell failed: " + (r.stderr or "")[-300:]
    return r

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

def totp(secret, when=None):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int((when if when is not None else time.time()) // 30))
    h = hmac.new(k, msg, hashlib.sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

OWNER = tok("demo-owner", PW); ORG = claims(OWNER)["org_id"]
OOWNER = tok("other-owner", PW)
ENG = tok("engineer1", PW)
r = api("GET", "/saloons/", token=OWNER)
sals = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
SALA = int([s for s in sals if s["name"] == "Saloon-A"][0].get("saloon_id") or [s for s in sals if s["name"] == "Saloon-A"][0]["id"])
r = api("GET", "/farms/", token=OWNER)
farms = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
FARM1 = [f for f in farms if f["name"] == "Farm1"][0].get("farm_id", [f for f in farms if f["name"] == "Farm1"][0].get("id"))

# 22.2 invalid since
r = api("GET", f"/saloons/{SALA}/telemetry/?since=not-a-date", token=OWNER)
R.ok("22.2", r["status"] == 200 and isinstance((r["json"] or {}).get("results"), list), f"{r['status']}")

# 22.3 repeat ack
r = api("GET", "/alerts/", token=OWNER)
alerts = (r["json"] or {}).get("results", [])
open_a = [a for a in alerts if not a.get("resolved_at")]
assert open_a, "no open alert for 22.3/22.4"
AID = open_a[0]["alert_id"]
r1 = api("POST", f"/alerts/{AID}/acknowledge/", token=OWNER, body={"notes": "qa-22.3a"})
r2 = api("POST", f"/alerts/{AID}/acknowledge/", token=OWNER, body={"notes": "qa-22.3b"})
R.ok("22.3", r1["status"] == 200 and r2["status"] == 200 and (r2["json"] or {}).get("status") == "ACKNOWLEDGED",
     f"{r1['status']}/{r2['status']} {r2['json']}")

# staff session for 22.4
r = api("POST", "/auth/login/", body={"username": "support1", "password": PW, "mfa_code": totp(MFA_SECRET)})
assert r["status"] == 200, r
ST = r["json"]["access_token"]
r = api("POST", "/platform/support/session/", token=ST, body={"org_id": ORG, "incident_id": f"QA-224-{RUN}"})
assert r["status"] == 200, r["json"]
SST = r["json"]["access_token"]

# 22.4 repeat force-resolve
f1 = api("POST", f"/platform/support/alerts/{AID}/force-resolve/", token=SST, body={"reason": "qa defect ENG-118, INC-224"})
f2 = api("POST", f"/platform/support/alerts/{AID}/force-resolve/", token=SST, body={"reason": "qa defect ENG-118, INC-224"})
R.ok("22.4", f1["status"] == 200 and (f1["json"] or {}).get("resolved") is True
     and f2["status"] == 200 and (f2["json"] or {}).get("idempotent") is True,
     f"{f1['status']}/{f2['status']} {f2['json']}")

# 22.5 re-invite deactivated member (offboarded in Other Co)
r = api("POST", "/org/members/", token=OOWNER, body={"username": "offboarded", "role": "worker"})
R.ok("22.5", r["status"] == 200 and (r["json"] or {}).get("is_active") in (True, "true", 1) or (r["status"] == 200 and contains(r["json"], "offboarded")),
     f"{r['status']} {r['json']}")
r = api("GET", "/org/members/", token=OOWNER)
mems = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
OBM = [m for m in mems if m["username"] == "offboarded"][0]
OBMID = OBM.get("member_id", OBM.get("id"))
r = api("POST", f"/org/members/{OBMID}/deactivate/", token=OOWNER)
assert r["status"] == 200, (OBMID, r)

# 22.6 touching mute windows
t0 = datetime.now(timezone.utc).replace(microsecond=0)
iso = lambda d: (t0 + d).isoformat().replace("+00:00", "Z")
a = api("POST", "/maintenance-mutes/", token=OWNER,
        body={"saloon_id": SALA, "starts_at": iso(timedelta(hours=5)), "ends_at": iso(timedelta(hours=7)), "reason": f"qa-22.6a-{RUN}"})
assert a["status"] == 201, a
ma = (a["json"] or {}).get("mute_id", (a["json"] or {}).get("id"))
b = api("POST", "/maintenance-mutes/", token=OWNER,
        body={"saloon_id": SALA, "starts_at": iso(timedelta(hours=7)), "ends_at": iso(timedelta(hours=9)), "reason": f"qa-22.6b-{RUN}"})
R.ok("22.6", b["status"] in (200, 201), f"A={a['status']} B={b['status']} {b['json']}")
mb = (b["json"] or {}).get("mute_id", (b["json"] or {}).get("id")) if b["status"] in (200, 201) else None
api("DELETE", f"/maintenance-mutes/{ma}/", token=OWNER)
if mb:
    api("DELETE", f"/maintenance-mutes/{mb}/", token=OWNER)

# 22.7 staff without session
r = api("GET", "/farms/", token=ST)
ok_farms = r["status"] == 400 and contains(r["json"], "organization context required")
r = api("GET", f"/saloons/{SALA}/alerts/", token=ST)
ok_alerts = r["status"] == 200 and (r["json"] or {}).get("results") == []
R.ok("22.7", ok_farms and ok_alerts, f"farms={ok_farms} alerts={ok_alerts}")

# 22.8 unknown compat slug
r = api("GET", "/api/compat/v1/farms/no-such-xyz/salons/no-such-abc/reports/", token=OWNER)
R.ok("22.8", r["status"] == 404, f"{r['status']} {str(r['json'])[:120]}")

# 22.9 refresh without rotation
r = api("POST", "/auth/login/", body={"username": "worker1", "password": PW})
assert r["status"] == 200, r
W1R = r["json"]["refresh_token"]
q1 = api("POST", "/auth/token/refresh/", body={"refresh": W1R})
q2 = api("POST", "/auth/token/refresh/", body={"refresh": W1R})
R.ok("22.9", q1["status"] == 200 and q2["status"] == 200
     and "access" in (q1["json"] or {}) and "refresh" not in (q1["json"] or {})
     and "access" in (q2["json"] or {}) and "refresh" not in (q2["json"] or {}),
     f"{q1['status']}/{q2['status']} keys1={sorted((q1['json'] or {}).keys())}")

# 22.12 role change: request 200, old token tail-valid, refresh revoked; restore + relogin
r = api("POST", "/auth/login/", body={"username": "reviewer1", "password": PW})
assert r["status"] == 200, r
R1T, R1R = r["json"]["access_token"], r["json"]["refresh_token"]
r = api("GET", "/org/members/", token=OWNER)
mems = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
R1M = [m for m in mems if m["username"] == "reviewer1"][0]
R1MID = R1M.get("member_id", R1M.get("id"))
r = api("PATCH", f"/org/members/{R1MID}/", token=OWNER, body={"role": "manager"})
ok_req = r["status"] == 200
r = api("GET", "/farms/", token=R1T)
ok_tail = r["status"] == 200
r = api("POST", "/auth/token/refresh/", body={"refresh": R1R})
ok_rev = r["status"] == 401 and contains(r["json"], "session revoked")
R.ok("22.12", ok_req and ok_tail and ok_rev, f"req={ok_req} tail={ok_tail} revoked={ok_rev}")
r = api("PATCH", f"/org/members/{R1MID}/", token=OWNER, body={"role": "reviewer"})
assert r["status"] == 200, r["json"]
r = api("POST", "/auth/login/", body={"username": "reviewer1", "password": PW})
assert r["status"] == 200, r

# 22.11 redis-down guard: A/B spare saloons (QA residue, documented)
mk = djshell("from apps.tenants.models import Organization\nfrom apps.farms.models import Farm, Saloon\n"
             "o=Organization.objects.get(name='Demo Farm'); f=Farm.objects.get(org=o, name='Farm1')\n"
             "a,_=Saloon.objects.get_or_create(org=o, farm=f, name='Saloon-QA2211A-" + RUN + "')\n"
             "b,_=Saloon.objects.get_or_create(org=o, farm=f, name='Saloon-QA2211B-" + RUN + "')\n"
             "print(a.id, b.id)")
S1, S2 = mk.stdout.strip().splitlines()[-1].split()
r = api("POST", f"/saloons/{S1}/operational-state/", token=ENG, body={"state": "PRE_HEATING"})
base_ok = r["status"] == 200
subprocess.run(["docker", "stop", "morghdari-redis"], capture_output=True)
try:
    t0w = time.time(); pong = False
    r = api("POST", f"/saloons/{S2}/operational-state/", token=ENG, body={"state": "PRE_HEATING"})
    down_ok = r["status"] == 200 and contains(r["json"], "PRE_HEATING")
    down_body = f"{r['status']} {r['json']}"
finally:
    subprocess.run(["docker", "start", "morghdari-redis"], capture_output=True)
    t1 = time.time()
    while time.time() - t1 < 120:
        time.sleep(5)
        try:
            if redis("PING") == "PONG":
                break
        except Exception:
            pass
R.ok("22.11", base_ok and down_ok, f"baseline={base_ok} redis-down={down_ok} {down_body}")

# 22.13 prometheus-down status page
subprocess.run(["docker", "stop", "morghdari-prometheus"], capture_output=True)
time.sleep(8)
try:
    r = api("GET", "/status/")
    ok13 = r["status"] == 200 and (r["json"] or {}).get("status") == "unknown"
    body13 = f"{r['status']} {str(r['json'])[:150]}"
finally:
    subprocess.run(["docker", "start", "morghdari-prometheus"], capture_output=True)
R.ok("22.13", ok13, body13)

# 22.15 Persian farm create + audit echo (ASCII-safe md5: psql bytes decode cp1252 on win)
import hashlib as _hl
want_md5 = _hl.md5("مرغداری طوس".encode("utf-8")).hexdigest()
geo = djshell("from apps.farms.models import Farm\nf=Farm.objects.filter(name='Farm1').first()\nprint(f.province_id, f.city_id)")
PROV, CITY = geo.stdout.strip().splitlines()[-1].split()
r = api("POST", "/farms/", token=OWNER, body={"name": "مرغداری طوس", "province_id": int(PROV), "city_id": int(CITY)})
fid = (r["json"] or {}).get("farm_id", (r["json"] or {}).get("id")) if r["status"] == 201 else None
ok15 = r["status"] == 201 and contains(r["json"], "مرغداری طوس")
got_md5 = ""
if fid:
    p = psql("SELECT md5(details->>'name') FROM account_activity_logs WHERE action='FARM_CREATED' AND (details->>'name') IS NOT NULL ORDER BY id DESC LIMIT 1")
    got_md5 = p.stdout.strip()
    ok15 = ok15 and got_md5 == want_md5
    d = api("DELETE", f"/farms/{fid}/", token=OWNER)
    ok15s = f"create={r['status']} audit-md5={got_md5 == want_md5} delete={d['status']} {str(d['json'])[:80]}"
else:
    ok15s = f"create={r['status']} {str(r['json'])[:150]}"
R.ok("22.15", ok15, ok15s)

R.f.flush()
print("WAVE9B DONE", flush=True)
