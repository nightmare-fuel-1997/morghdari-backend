"""Live probe: BUG-13 port — poison (overflow) + malformed (bad float) entries DLQ+XACK on the fresh worker."""
import json, os, re, subprocess, sys, time, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
OWNER = tok("demo-owner", "password12345"); ORG = claims(OWNER)["org_id"]

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

def dlq_after(after_id):
    out = redis("XRANGE", "telemetry:dead_letter", f"({after_id}", "+", "COUNT", "30") or ""
    entries, cur, txt = [], None, []
    for line in out.splitlines():
        s = line.strip()
        mm = re.search(r"(\d+-\d+)$", s)
        if mm and len(s) < 40:
            if cur is not None:
                entries.append((cur, "\n".join(txt)))
            cur, txt = mm.group(1), []
        elif cur is not None:
            txt.append(s)
    if cur is not None:
        entries.append((cur, "\n".join(txt)))
    return entries

def pending(eid):
    out = redis("XPENDING", "telemetry:realtime", "telemetry_workers", eid, eid, "1") or ""
    if not out or "empty" in out.lower() or eid not in out:
        return None
    return out

tail0 = redis("XREVRANGE", "telemetry:dead_letter", "+", "-", "COUNT", "1") or ""
m0 = re.search(r"(\d+-\d+)", tail0)
tail0id = m0.group(1) if m0 else "0-0"

def xadd(recv_ts, marker):
    from datetime import datetime, timezone
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    eid = redis("XADD", "telemetry:realtime", "*",
                "org_id", ORG, "farm_id", "1", "gateway_slot_id", "GW-QA0001",
                "device_ts", dts, "is_buffered", "False", "slot_id", "SS-00010",
                "payload", json.dumps({"qa": marker}), "recv_ts", recv_ts,
                "trace_id", marker, "metrics", '{"temp": 20.0}', "clock_drift", "False")
    return (eid or "").strip(), marker

peid, pmark = xadd("9999999999.0", f"qa-port-poison-{uuid.uuid4().hex[:6]}")
meid, mmark = xadd("not-a-float", f"qa-port-malformed-{uuid.uuid4().hex[:6]}")

found = {}
t0 = time.time()
while time.time() - t0 < 120:
    time.sleep(5)
    for did, txt in dlq_after(tail0id):
        if pmark in txt and "PROCESSING_FAILED" in txt:
            found["poison"] = did
        if mmark in txt and "PROCESSING_ERROR" in txt:
            found["malformed"] = did
    if len(found) == 2:
        break

p_gone = pending(peid) is None
m_gone = pending(meid) is None
R.ok("17.1-port-poison", "poison" in found and p_gone, f"DLQ {found.get('poison')} PROCESSING_FAILED, xacked={p_gone}")
R.ok("17.1-port-malformed", "malformed" in found and m_gone, f"DLQ {found.get('malformed')} PROCESSING_ERROR, xacked={m_gone}")
R.f.flush()
print("PORTPROBE DONE", flush=True)
