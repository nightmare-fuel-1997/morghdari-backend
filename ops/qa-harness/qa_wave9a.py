"""Wave 9a — section 21 (negative/security edges 21.1-21.20)."""
import base64
import hashlib
import hmac
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, contains
import ssl as _ssl

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

def djshell(py, inp=None):
    r = subprocess.run(["docker", "exec", "-i",
                        "-e", "DATABASE_URL=postgres://%s:%s@postgres:5432/%s" % (
                            env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"]),
                        "morghdari-daphne", "python", "manage.py", "shell", "-c", py] if inp is None else
                       ["docker", "exec", "-i",
                        "-e", "DATABASE_URL=postgres://%s:%s@postgres:5432/%s" % (
                            env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"]),
                        "-e", "REDIS_URL=redis://:%s@redis:6379" % env1["REDIS_PASSWORD"],
                        "morghdari-daphne", "python", "manage.py", "shell"],
                       capture_output=True, text=True, input=inp)
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
OOWNER = tok("other-owner", PW); OORG = claims(OOWNER)["org_id"]
MGR = tok("manager1", PW); ENG = tok("engineer1", PW); INST = tok("installer1", PW)
api("DELETE", "/maintenance-mutes/10/", token=MGR)  # orphan from the mute_id-keyed run

# discovery
r = api("GET", "/farms/", token=OWNER); assert r["status"] == 200, r
farms = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
FARM1 = [f for f in farms if f["name"] == "Farm1"][0].get("farm_id", [f for f in farms if f["name"] == "Farm1"][0].get("id"))
r = api("GET", "/saloons/", token=OWNER); assert r["status"] == 200, r
sals = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
SALA = int([s for s in sals if s["name"] == "Saloon-A"][0].get("saloon_id") or [s for s in sals if s["name"] == "Saloon-A"][0].get("id"))
r = api("GET", "/saloons/", token=OOWNER); assert r["status"] == 200, r
osals = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
SALX = int([s for s in osals if s["name"] == "Saloon-X"][0].get("saloon_id") or [s for s in osals if s["name"] == "Saloon-X"][0].get("id"))
r = api("GET", "/farms/", token=OOWNER); assert r["status"] == 200, r
ofarms = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
FARMO = [f for f in ofarms if f["name"] == "FarmOther"][0].get("farm_id", [f for f in ofarms if f["name"] == "FarmOther"][0].get("id"))
r = api("GET", "/org/members/", token=OOWNER); assert r["status"] == 200, r
omems = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
OMEM = [m for m in omems if m["username"] in ("other-owner",)][0].get("member_id", [m for m in omems if m["username"] in ("other-owner",)][0].get("id"))
FLOCK = djshell("from apps.telemetry.models import FlockCycle\nprint(FlockCycle.objects.filter(saloon_id=%d, end_date__isnull=True).values_list('flock_id', flat=True).first())" % SALA).stdout.strip().splitlines()[-1]
print("ids", FARM1, SALA, SALX, FARMO, OMEM, FLOCK, flush=True)

# 21.1 login without password
r = api("POST", "/auth/login/", body={"username": "demo-owner"})
R.ok("21.1", r["status"] in (400, 401) and not (r["json"] or {}).get("access_token"), f"{r['status']} {r['json']}")

# 21.2 zero-membership login
r = api("POST", "/auth/login/", body={"username": "offboarded", "password": PW})
R.ok("21.2", r["status"] == 401 and contains(r["json"], "no active org membership"), f"{r['status']} {r['json']}")

# 21.3 staff login without TOTP
r = api("POST", "/auth/login/", body={"username": "support1", "password": PW})
R.ok("21.3", r["status"] == 403 and contains(r["json"], "mfa_required"), f"{r['status']} {r['json']}")

# 21.5 cross-org reads
r = api("GET", f"/saloons/{SALX}/telemetry/", token=OWNER)
R.ok("21.5a", r["status"] == 200 and (r["json"] or {}).get("results") == [], f"{r['status']} {str(r['json'])[:100]}")
r = api("GET", f"/farms/{FARMO}/telemetry/", token=OWNER)
R.ok("21.5b", r["status"] == 404 and contains(r["json"], "farm not found"), f"{r['status']} {r['json']}")
# other-org report: create one as other-owner, then touch as demo-owner
today = datetime.now(timezone.utc).date().isoformat()
OFLOCK = djshell("from apps.telemetry.models import FlockCycle\nprint(FlockCycle.objects.filter(saloon_id=%d, end_date__isnull=True).values_list('flock_id', flat=True).first())" % SALX).stdout.strip().splitlines()[-1]
r = api("POST", f"/saloons/{SALX}/reports/", token=OOWNER,
        body={"flock_id": OFLOCK, "report_date": today, "mortality_count": 0})
orep = (r["json"] or {}).get("id") if r["status"] == 201 else None
assert orep, r
r = api("PATCH", f"/reports/{orep}/", token=OWNER, body={"mortality_count": 1})
R.ok("21.5c", r["status"] == 404 and contains(r["json"], "report not found"), f"{r['status']} {r['json']}")
r = api("POST", f"/org/members/{OMEM}/deactivate/", token=OWNER)
R.ok("21.5d", r["status"] == 404 and contains(r["json"], "no such member"), f"{r['status']} {r['json']}")
djshell("from apps.reports.models import DailyReport\nDailyReport.objects.filter(pk=%s).delete(); print('gone')" % orep)

# 21.14 limit bound
r = api("GET", f"/saloons/{SALA}/telemetry/?limit=1001", token=OWNER)
R.ok("21.14", r["status"] == 400 and contains(r["json"], "limit must be between 1 and 1000"), f"{r['status']} {r['json']}")

# 21.15 /metrics from outside (nginx)
r = api("GET", "/metrics/")
R.ok("21.15", r["status"] == 404, f"{r['status']}")

# 21.6 mute > 72h
t0 = datetime.now(timezone.utc).replace(microsecond=0)
iso = lambda d: (t0 + d).isoformat().replace("+00:00", "Z")
r = api("POST", "/maintenance-mutes/", token=MGR,
        body={"saloon_id": SALA, "starts_at": iso(timedelta(hours=1)), "ends_at": iso(timedelta(hours=101)), "reason": "qa-21.6"})
R.ok("21.6", r["status"] == 400 and contains(r["json"], "72 hours"), f"{r['status']} {r['json']}")

# 21.7 overlapping mute windows
r = api("POST", "/maintenance-mutes/", token=MGR,
        body={"saloon_id": SALA, "starts_at": iso(timedelta(hours=1)), "ends_at": iso(timedelta(hours=3)), "reason": "qa-21.7a"})
mid = (r["json"] or {}).get("mute_id", (r["json"] or {}).get("id")) if r["status"] == 201 else None
assert mid, r
r = api("POST", "/maintenance-mutes/", token=MGR,
        body={"saloon_id": SALA, "starts_at": iso(timedelta(hours=2)), "ends_at": iso(timedelta(hours=4)), "reason": "qa-21.7b"})
R.ok("21.7", r["status"] == 409 and contains(r["json"], "overlaps"), f"{r['status']} {r['json']}")
api("DELETE", f"/maintenance-mutes/{mid}/", token=MGR)

# 21.8 ring jump ACTIVE_FLOCK -> PRE_HEATING
r = api("POST", f"/saloons/{SALA}/operational-state/", token=ENG, body={"state": "PRE_HEATING"})
R.ok("21.8", r["status"] == 400 and contains(r["json"], "illegal transition"), f"{r['status']} {r['json']}")

# 21.12 + 21.13 report windows (today row, conflict first, then backdate)
djshell("from apps.reports.models import DailyReport\nprint(DailyReport.objects.filter(flock_id=%s, saloon_id=%d, report_date='%s').delete())" % (FLOCK, SALA, today))
r = api("POST", f"/saloons/{SALA}/reports/", token=ENG,
        body={"flock_id": FLOCK, "report_date": today, "mortality_count": 1})
rid = (r["json"] or {}).get("id") if r["status"] == 201 else None
assert rid, r
r = api("PATCH", f"/reports/{rid}/", token=ENG, body={"mortality_count": 3, "casualties_today": 5})
R.ok("21.13", r["status"] == 400 and contains(r["json"], "conflicting fields"), f"{r['status']} {r['json']}")
yd = None
for d in (1, 2, 3, 5, 8):
    cand = (datetime.now(timezone.utc) - timedelta(days=d)).date().isoformat()
    n = djshell("from apps.reports.models import DailyReport\nprint(DailyReport.objects.filter(flock_id=%s, saloon_id=%d, report_date='%s').count())" % (FLOCK, SALA, cand)).stdout.strip().splitlines()[-1]
    if n == "0":
        yd = cand
        break
assert yd, "no free past date for 21.12 fixture"
djshell("from apps.reports.models import DailyReport\nDailyReport.objects.filter(pk=%s).update(report_date='%s'); print('backdated')" % (rid, yd))
r = api("PATCH", f"/reports/{rid}/", token=ENG, body={"mortality_count": 6})
R.ok("21.12", r["status"] == 403 and contains(r["json"], "edit window closed"), f"{r['status']} {r['json']}")
djshell("from apps.reports.models import DailyReport\nDailyReport.objects.filter(pk=%s).delete(); print('gone')" % rid)

# 21.9 gateway-request cap (cap=5 live): fill to cap, assert cap+1 -> 400, staff-reject created
created = []
st400 = None
for i in range(8):
    r = api("POST", "/gateway-requests/", token=MGR, body={"farm_id": FARM1, "details": {"qa": f"21.9-{RUN}-{i}"}})
    if r["status"] == 201:
        created.append((r["json"] or {}).get("id"))
    else:
        st400 = r
        break
R.ok("21.9", st400 is not None and st400["status"] == 400 and contains(st400["json"], "pending gateway request cap reached"),
     f"created={len(created)} last={st400['status'] if st400 else None} {st400['json'] if st400 else None}")
r = api("POST", "/auth/login/", body={"username": "support1", "password": PW, "mfa_code": totp(MFA_SECRET)})
assert r["status"] == 200, r
ST = r["json"]["access_token"]
r = api("POST", "/platform/support/session/", token=ST, body={"org_id": ORG, "incident_id": f"QA-219-{RUN}"})
assert r["status"] == 200, r["json"]
SST = r["json"]["access_token"]
for gid in created:
    rr = api("POST", f"/gateway-requests/{gid}/review/", token=SST, body={"decision": "REJECT"})
    assert rr["status"] == 200, (gid, rr)

# 21.19 second concurrent sync (pre-held lock, zero side effects)
redis("SET", "lock:sync:GW-QA0001", "qa-probe", "NX", "EX", "30")
r = api("POST", "/provisioning/sync/", token=INST, body={"gateway_slot_id": "GW-QA0001", "zones": [], "sensor_slot_mappings": []}, full=False)
R.ok("21.19", r["status"] == 409 and contains(r["json"], "already in progress"),
     f"{r['status']} {r['json']}")
redis("DEL", "lock:sync:GW-QA0001")

# 21.20 support action outside suspend/resume
r = api("POST", "/platform/support/gateways/GW-QA0001/provisioning/", token=SST, body={"action": "reboot"})
R.ok("21.20", r["status"] == 400 and contains(r["json"], "suspend or resume"), f"{r['status']} {r['json']}")

# 21.4 refresh after role revoke (victim worker1)
r = api("POST", "/auth/login/", body={"username": "worker1", "password": PW})
assert r["status"] == 200, r
W1R = r["json"]["refresh_token"] if "refresh_token" in (r["json"] or {}) else r["json"].get("refresh")
r = api("GET", "/org/members/", token=OWNER)
mems = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
W1M = [m for m in mems if m["username"] == "worker1"][0].get("member_id", [m for m in mems if m["username"] == "worker1"][0].get("id"))
r = api("PATCH", f"/org/members/{W1M}/", token=OWNER, body={"role": "manager"})
assert r["status"] == 200, r["json"]
r = api("POST", "/auth/token/refresh/", body={"refresh": W1R})
R.ok("21.4", r["status"] == 401 and contains(r["json"], "session revoked"), f"{r['status']} {r['json']}")
r = api("PATCH", f"/org/members/{W1M}/", token=OWNER, body={"role": "worker"})
assert r["status"] == 200, r["json"]
r = api("POST", "/auth/login/", body={"username": "worker1", "password": PW})
assert r["status"] == 200, r

# 21.16 WS cross-org token -> wire 403 (BUG-6) + server AUTH-DENY 4003
OT = tok("other-owner", PW)
s = _ssl._create_unverified_context().wrap_socket(
    socket.create_connection(("127.0.0.1", 443), timeout=15), server_hostname="localhost")
req = (f"GET /ws/saloons/{SALA}/telemetry?token={OT} HTTP/1.1\r\nHost: localhost\r\n"
       "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
       "Sec-WebSocket-Version: 13\r\n\r\n")
s.sendall(req.encode()); s.settimeout(10)
try:
    resp = s.recv(4096).decode("latin1")
finally:
    s.close()
line1 = resp.split("\r\n")[0]
logs = subprocess.run(["docker", "logs", "--since", "120s", "morghdari-daphne"],
                      capture_output=True, text=True).stdout + subprocess.run(
                      ["docker", "logs", "--since", "120s", "morghdari-daphne"],
                      capture_output=True, text=True).stderr
deny = ("code=4003" in logs) or ("AUTH-DENY" in logs)
R.ok("21.16", line1.startswith("HTTP/1.1 403") and deny, f"{line1} deny-logged={deny}")

# 21.17 / 21.18 / 22.1 / 22.10 via the REAL pipeline in daphne shell
probe = (
    "import json, os\n"
    "import redis as redis_lib\n"
    "from morghdari.subscriber.pipeline import handle_message\n"
    "r = redis_lib.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)\n"
    "ORG = '" + ORG + "'\n"
    "TS = 'QA-" + RUN + "'\n"
    "def pay(slot, ts, schema='2.0'):\n"
    "    return json.dumps({'gateway_hardware_id': 'HW-' + slot, 'gateway_slot_id': slot,\n"
    "        'schema_version': schema, 'device_ts': ts, 'is_buffered': False,\n"
    "        'readings': [{'pack_id': 'P-A', 'metrics': {'temp': {'avg': 28.4}}}]}).encode()\n"
    "stub = lambda *a: 'SS-QA-STUB'\n"
    "x0 = r.xlen('telemetry:realtime'); d0 = r.xlen('telemetry:dead_letter')\n"
    "# 21.18 bad schema\n"
    "e18 = handle_message(r, ORG + '/farms/1/gateways/GW-QA0001/telemetry', pay('GW-QA0001', TS + '-18', '1.9'), db_fetch=stub, status_fetch=lambda gw: 'PROVISIONED')\n"
    "print('E18', e18 is not None)\n"
    "print('E18R', r.xrange('telemetry:dead_letter', min='-', max='+')[-1][1].get('reason'))\n"
    "# 21.17 suspended\n"
    "e17 = handle_message(r, ORG + '/farms/1/gateways/GW-QASUSP/telemetry', pay('GW-QASUSP', TS + '-17'), db_fetch=stub, status_fetch=lambda gw: 'SUSPENDED')\n"
    "print('E17', e17 is not None)\n"
    "print('E17R', r.xrange('telemetry:dead_letter', min='-', max='+')[-1][1].get('reason'))\n"
    "# 22.10 no record flows\n"
    "xA = r.xlen('telemetry:realtime')\n"
    "e10 = handle_message(r, ORG + '/farms/1/gateways/GW-QA0001/telemetry', pay('GW-QA0001', TS + '-10'), db_fetch=stub, status_fetch=None)\n"
    "print('E10', e10, r.xlen('telemetry:realtime') - xA)\n"
    "# 22.1 dedup\n"
    "xB = r.xlen('telemetry:realtime')\n"
    "p1 = pay('GW-QA0001', TS + '-01')\n"
    "a1 = handle_message(r, ORG + '/farms/1/gateways/GW-QA0001/telemetry', p1, db_fetch=stub, status_fetch=None)\n"
    "xC = r.xlen('telemetry:realtime')\n"
    "a2 = handle_message(r, ORG + '/farms/1/gateways/GW-QA0001/telemetry', p1, db_fetch=stub, status_fetch=None)\n"
    "xD = r.xlen('telemetry:realtime')\n"
    "print('E01', a1, a2, xC - xB, xD - xC)\n"
)
pr = djshell(None, inp=probe)
out = pr.stdout
def got(tag):
    for ln in out.splitlines():
        if ln.startswith(tag + " "):
            return ln[len(tag) + 1:]
    return "MISSING"
e18, e18r = got("E18") == "True", got("E18R")
e17, e17r = got("E17") == "True", got("E17R")
R.ok("21.18", e18 and e18r == "UNSUPPORTED_SCHEMA_VERSION", f"dlq={e18} reason={e18r}")
R.ok("21.17", e17 and e17r == "gateway_suspended", f"dlq={e17} reason={e17r}")
e10 = got("E10").split()
R.ok("22.10", e10[0] == "None" and e10[1] == "1", f"E10={got('E10')}")
e01 = got("E01").split()
R.ok("22.1", e01 == ["None", "None", "1", "0"], f"E01={got('E01')}")

# 21.10 throttle (fresh window: 3x401 then 429) — worker1 fresh login above
W1T = tok("worker1", PW)
codes = []
for i in range(4):
    r = api("POST", "/auth/password/change/", token=W1T,
            body={"current_password": "wrong-pw", "new_password": f"whatever-pass-{RUN}-{i}"})
    codes.append(r["status"])
R.ok("21.10", codes[:3] == [401, 401, 401] and codes[3] == 429, f"{codes}")
print("sleeping 65s for throttle window reset...", flush=True)
time.sleep(65)

# 21.11 short password (fresh window)
r = api("POST", "/auth/password/change/", token=W1T,
        body={"current_password": PW, "new_password": "shrt9ch!x"})
R.ok("21.11", r["status"] == 400 and contains(r["json"], "min 10"), f"{r['status']} {r['json']}")

R.f.flush()
print("WAVE9A DONE", flush=True)
