"""Quick: 20.5b (saloon-7 feed), 12.8 (gateway zone), 17.1 (manual XADD)."""
import json, os, re, subprocess, sys, threading, time
import urllib.request
import urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, CTX

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

OWNER = tok("demo-owner", PW := "password12345")
ENG = tok("engineer1", PW)

# 20.5b: los row visible in saloon-7 feed
r = api("GET", "/alerts/?saloon_id=7", token=ENG)
j = r["json"] or {}
lst = j.get("results", j) if isinstance(j, dict) else j
has_los = any((x.get("metric_type") or x.get("metric")) == "los" for x in lst)
R.ok("20.5b", r["status"] == 200 and has_los, f"feed 200, los present={has_los} (metric=los choice itself 400s)")

# 12.8: gateway zone 1r/s burst 5
codes = []
def hit():
    req = urllib.request.Request("https://localhost/api/v1/gateways/")
    try:
        r = urllib.request.urlopen(req, timeout=20, context=CTX)
        codes.append(r.status)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
    except Exception as e:
        codes.append(type(e).__name__)
ts = [threading.Thread(target=hit) for _ in range(30)]
[t.start() for t in ts]; [t.join() for t in ts]
from collections import Counter
cc = Counter(codes)
R.ok("12.8", 429 in codes and 200 in codes, f"{dict(cc)} (1r/s burst=5 trips)")

# 17.1: manual XADD -> DB row
ORG = claims(OWNER)["org_id"]
c0 = int(psql("SELECT count(*) FROM saloon_telemetry").stdout.strip())
eid = redis("XADD", "telemetry:realtime", "*", "org_id", ORG, "farm_id", "1",
            "gateway_slot_id", "GW-QA0001", "device_ts", "2026-01-01T00:00:00Z",
            "is_buffered", "False", "slot_id", "SS-00010",
            "payload", '{"manual": true}', "recv_ts", "9999999999.0",
            "trace_id", "qa-171", "metrics", '{"temp": 20.0}', "clock_drift", "False")
t0 = time.time(); ok = False
while time.time() - t0 < 30:
    c1 = int(psql("SELECT count(*) FROM saloon_telemetry").stdout.strip())
    if c1 > c0:
        ok = True; break
    time.sleep(2)
R.ok("17.1", ok, f"manual XADD {eid[:8] if eid else '?'} -> DB in <30s")
R.f.flush()
print("QUICK DONE", flush=True)
