"""QA harness for qa-test-plan.html waves — no third-party deps.

Usage: python scripts/qa_lib.py (self-check) or import in per-wave scripts.
Results land in results.jsonl: one {id, verdict, observed} per test.
"""
import json, ssl, sys, time, urllib.error, urllib.request

BASE = "https://localhost/api/v1"
CTX = ssl._create_unverified_context()
import time as _t
_last = [0.0]
def _pace():
    # nginx `api` zone = 5r/s per client; stay under it so harness traffic
    # never 429s itself (wave 2 proved the cascade hides real verdicts).
    d = 0.25 - (_t.time() - _last[0])
    if d > 0:
        _t.sleep(d)
    _last[0] = _t.time()
import os as _os
RESULTS = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "qa_results.jsonl")

def api(method, path, token=None, body=None, raw=False, headers=None, full=False):
    _pace()
    url = path if path.startswith("http") else BASE + path
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            code, bodyb = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, bodyb = e.code, e.read()
    except urllib.error.URLError as e:
        return {"status": 0, "json": None, "text": str(e), "reason": "URLERROR"}
    out = {"status": code}
    try:
        j = json.loads(bodyb)
        out["json"] = j
    except Exception:
        out["json"] = None
        out["text"] = bodyb.decode("utf-8", "replace")[:400]
    if raw:
        out["bytes"] = len(bodyb)
    return out

def tok(user, pw):
    r = api("POST", "/auth/login/", body={"username": user, "password": pw})
    if r["status"] != 200:
        raise SystemExit(f"login {user} failed: {r}")
    return r["json"]["access_token"]

def claims(t):
    import base64
    p = t.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))

class Rec:
    def __init__(self):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        self.f = open(RESULTS, "a", encoding="utf-8")
    def v(self, tid, verdict, observed=""):
        line = {"id": tid, "verdict": verdict, "observed": str(observed)[:300]}
        print(f"{verdict:5} {tid:8} {str(observed)[:120]}")
        self.f.write(json.dumps(line, ensure_ascii=False) + "\n")
    def ok(self, tid, cond, observed=""):
        self.v(tid, "PASS" if cond else "FAIL", observed)

def contains(hay, needle):
    if isinstance(hay, (dict, list)):
        hay = json.dumps(hay, ensure_ascii=False)
    return needle in (hay or "")

if __name__ == "__main__":
    r = api("GET", "/health/")
    print("selfcheck", r["status"], (r.get("json") or {}).get("status"))
    print("login", tok("demo-owner", "password12345")[:20])
