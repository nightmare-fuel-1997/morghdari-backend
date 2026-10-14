"""Wave 11 — Task A re-probes (NO product code changes).

Probes (append to qa_results.jsonl ONLY on fresh PASS, one line per id):
  16.13a (BUG-2):  GET /reconciliation/ with NO ?day= -> 200, day-shaped body.
  smoke-6 (BUG-4): hil-readiness leg the way vps_smoke.sh runs it -> green.
  12.7 (BUG-11):   GW Basic -> 200; manager1 JWT -> 200; bad creds -> 401 msg.
  17.1 variant (BUG-13): poison realtime entry -> DLQ w/ reason + XACK'd.

Bounded waits only. If the quarantine is absent the poison entry is
XACK+XDEL'd after the observation window so the stack is left un-spammed.
"""
import base64
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, contains, CTX

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
env1 = dict(m.groups() for line in open(os.path.join(ROOT, ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
RUN = str(int(time.time()))[-6:]

VERDICTS = {}  # id -> (bool, observed)


def report(tid, cond, observed):
    VERDICTS[tid] = (bool(cond), str(observed)[:300])
    print(f"{'PASS' if cond else 'FAIL':5} {tid:8} {str(observed)[:150]}", flush=True)


def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
                           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"],
                           "-w", "-t", "-A", "-c", sql],
                          capture_output=True, text=True,
                          env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})


ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (
    env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])


def djshell(py):
    cmd = ["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
           "morghdari-daphne", "python", "manage.py", "shell", "-c", py]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, "shell failed: " + (r.stderr or "")[-300:]
    return r


def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()


OWNER = tok("demo-owner", PW)
ORG = claims(OWNER)["org_id"]

# ---- 1. 16.13a (BUG-2): no ?day= must default to tehran_yesterday ----
r = api("GET", "/reconciliation/", token=OWNER)
body = r["json"] if isinstance(r["json"], dict) else {}
ok13a = (r["status"] == 200 and "day" in body and "orgs" in body
         and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(body.get("day") or "")) is not None)
report("16.13a", ok13a,
       f"status={r['status']} day={body.get('day') if body else None} "
       f"orgs={len(body.get('orgs', [])) if body else 0} {str(r.get('text', ''))[:80]}")

# ---- 2. smoke-6 (BUG-4): hil-readiness leg as vps_smoke.sh runs it ----
# Windows rule: Linux pipelines only via Git-bash script files in .tmp-qa/.
# The shim mirrors scripts/qa_smoke.sh (python3 stub -> real interpreter).
HIL_SH = os.path.join(ROOT, ".tmp-qa", "qa11_hil.sh")
posix_py = sys.executable.replace("\\", "/")
sh_lines = (
    "#!/usr/bin/env bash\n"
    "set -u\n"
    'cd "$(dirname "$0")/.."\n'
    "mkdir -p /tmp/qabin\n"
    '{ echo "#!/bin/sh"; echo \'exec "' + posix_py + '" "$@"\'; } > /tmp/qabin/python3\n'
    "chmod +x /tmp/qabin/python3\n"
    'PATH="/tmp/qabin:$PATH"\n'
    'PYTHON_BIN="$(command -v python3 || command -v python)"\n'
    '"$PYTHON_BIN" scripts/check_hil_readiness.py --skip-live\n'
)
with open(HIL_SH, "w", encoding="utf-8", newline="\n") as f:
    f.write(sh_lines)
GITBASH = "C:\\Program Files\\Git\\bin\\bash.exe"
h = subprocess.run([GITBASH, ".tmp-qa/qa11_hil.sh"], capture_output=True, text=True, cwd=ROOT)
hout = ((h.stdout or "") + (h.stderr or "")).strip().replace("\n", " | ")
oksmoke = h.returncode == 0 and "0 fail" in hout
report("smoke-6", oksmoke, f"rc={h.returncode} {hout[-220:]}")


# ---- 3. 12.7 group (BUG-11): device Basic / manager JWT / bad creds ----
def gw_get(auth):
    req = urllib.request.Request("https://localhost/api/v1/gateways/GW-QA0001/config/",
                                 headers={"Authorization": auth} if auth else {})
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def basic(u, p):
    return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()


ba, ba_body = gw_get(basic("GW-QA0001", "GW-QA0001-mqtt-pw"))
time.sleep(1.5)  # gateway-config zone is 1r/s per IP; avoid 429 masking
MGR = tok("manager1", PW)
bb, bb_body = gw_get("Bearer " + MGR)
time.sleep(1.5)
bc, bc_body = gw_get(basic("GW-QA0001", "wrong"))
ok127 = (ba == 200 and bb == 200 and bc == 401
         and "gateway Basic credentials or manager role required" in bc_body)
report("12.7", ok127, f"basic={ba} mgr={bb} bad={bc} bad_body={bc_body[:90]}")


# ---- 4. 17.1 poison variant (BUG-13): absurd recv_ts -> DLQ + XACK ----
# NUMERIC(10,2) overflow: generated buffered_delay_mins = (server_received_ts
# - device_ts)/60 min; recv_ts year ~5138 vs device_ts 2026 overflows it.
MARKER = f"qa11-poison-{RUN}"
dlq0 = int(redis("XLEN", "telemetry:dead_letter") or 0)
tail0 = redis("XREVRANGE", "telemetry:dead_letter", "+", "-", "COUNT", "1")
m0 = re.search(r"(\d+-\d+)", tail0 or "")
tail0id = m0.group(1) if m0 else "0-0"
peid = redis("XADD", "telemetry:realtime", "*",
             "org_id", ORG, "farm_id", "1",
             "gateway_slot_id", "GW-QA0001", "device_ts", "2026-01-01T00:00:00Z",
             "is_buffered", "False", "slot_id", "SS-00010",
             "payload", '{"qa": "%s"}' % MARKER, "recv_ts", "9999999999.0",
             "trace_id", MARKER, "metrics", '{"temp": 20.0}', "clock_drift", "False")
peid = (peid or "").strip()
assert re.fullmatch(r"\d+-\d+", peid or ""), f"XADD failed: {peid!r}"


def dlq_entries_after(after_id):
    out = redis("XRANGE", "telemetry:dead_letter", f"({after_id}", "+", "COUNT", "30")
    entries, cur_id, cur_txt = [], None, []
    for line in (out or "").splitlines():
        s = line.strip()
        mm = re.search(r"(\d+-\d+)$", s)
        if mm and len(s) < 40:
            if cur_id is not None:
                entries.append((cur_id, "\n".join(cur_txt)))
            cur_id, cur_txt = mm.group(1), []
        elif cur_id is not None:
            cur_txt.append(s)
    if cur_id is not None:
        entries.append((cur_id, "\n".join(cur_txt)))
    return entries


def pending_deliveries(eid):
    out = redis("XPENDING", "telemetry:realtime", "telemetry_workers", eid, eid, "1")
    if not out or "empty" in out.lower() or eid not in out:
        return None  # acked / gone from PEL
    ints = [int(x) for x in re.findall(r"\(integer\) (\d+)", out)]
    return ints[-1] if ints else 0


dlq_hit, last_n = None, None
t0 = time.time()
while time.time() - t0 < 100:
    time.sleep(5)
    for did, txt in dlq_entries_after(tail0id):
        if MARKER in txt and "PROCESSING_FAILED" in txt:
            dlq_hit = (did, txt)
            break
    if dlq_hit:
        break
    last_n = pending_deliveries(peid)

acked = pending_deliveries(peid) is None
if dlq_hit and acked:
    reason = re.search(r"PROCESSING_FAILED", dlq_hit[1]).group(0)
    report("17.1", True,
           f"poison {peid} -> DLQ {dlq_hit[0]} reason={reason} xacked(absent from PEL)")
else:
    dlq1 = int(redis("XLEN", "telemetry:dead_letter") or 0)
    n_now = pending_deliveries(peid)
    obs = (f"poison {peid} NOT quarantined in 100s: dlq {dlq0}->{dlq1}, "
           f"PEL deliveries={n_now} (was {last_n}); ")
    # Restore: stop the unbounded redelivery loop the old code leaves behind.
    redis("XACK", "telemetry:realtime", "telemetry_workers", peid)
    redis("XDEL", "telemetry:realtime", peid)
    gone = pending_deliveries(peid) is None
    report("17.1", False, obs + f"cleaned XACK+XDEL, pel_gone={gone}")

# ---- record ONLY fresh PASS lines (last-per-id wins in qa_results.jsonl) ----
for tid, (cond, obs) in VERDICTS.items():
    if cond:
        R.v(tid, "PASS", obs)
R.f.flush()
print("WAVE11 DONE " + " ".join(f"{k}={'PASS' if v[0] else 'FAIL'}" for k, v in VERDICTS.items()),
      flush=True)
