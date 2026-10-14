"""Wave 10b — slow probes: 15.9 (nginx 300s idle WS close) + 4.8 (900s support-session expiry), threaded."""
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
import threading
import time
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

def totp(secret, when=None):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int((when if when is not None else time.time()) // 30))
    h = hmac.new(k, msg, hashlib.sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

OWNER = tok("demo-owner", PW); ORG = claims(OWNER)["org_id"]
r = api("GET", "/saloons/", token=OWNER)
sals = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
SALA = int([s for s in sals if s["name"] == "Saloon-A"][0].get("saloon_id") or [s for s in sals if s["name"] == "Saloon-A"][0]["id"])
res15 = {}
res48 = {}

def probe_159():
    """Open a WS, stay fully idle, expect nginx proxy_read_timeout 300s to close it."""
    try:
        s = _ssl._create_unverified_context().wrap_socket(
            socket.create_connection(("127.0.0.1", 443), timeout=20), server_hostname="localhost")
        req = (f"GET /ws/saloons/{SALA}/telemetry?token={OWNER} HTTP/1.1\r\nHost: localhost\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())
        s.settimeout(20)
        first = s.recv(4096).decode("latin1", "replace")
        if not first.startswith("HTTP/1.1 101"):
            res15["err"] = f"no upgrade: {first.splitlines()[0] if first else 'empty'}"
            s.close()
            return
        t_up = time.time()
        closed_at = None
        s.settimeout(340)
        while time.time() - t_up < 340:
            try:
                data = s.recv(4096)
            except socket.timeout:
                break
            except (ConnectionResetError, OSError):
                closed_at = time.time()
                break
            if not data:
                closed_at = time.time()
                break
            # any frame (hello/ping) just resets nothing — idle means client sends nothing
        try:
            s.close()
        except Exception:
            pass
        if closed_at is None:
            res15["err"] = "still open after 340s idle"
        else:
            res15["elapsed"] = round(closed_at - t_up, 1)
    except Exception as exc:
        res15["err"] = f"probe error: {exc}"

def probe_48():
    """Support session op works pre-expiry; after >=910s the same op 403s."""
    try:
        r = api("POST", "/auth/login/", body={"username": "support1", "password": PW, "mfa_code": totp(MFA_SECRET)})
        assert r["status"] == 200, r
        ST = r["json"]["access_token"]
        r = api("POST", "/platform/support/session/", token=ST, body={"org_id": ORG, "incident_id": f"QA-48-{RUN}"})
        assert r["status"] == 200, r["json"]
        SST = r["json"]["access_token"]
        aid = psql("SELECT id FROM alert_instances WHERE org_id='%s' ORDER BY triggered_at DESC LIMIT 1" % ORG).stdout.strip()
        assert aid, "no alert row for 4.8 op"
        pre = api("POST", f"/platform/support/alerts/{aid}/force-resolve/", token=SST, body={"reason": f"qa 4.8 pre {RUN}"})
        time.sleep(915)
        post = api("POST", f"/platform/support/alerts/{aid}/force-resolve/", token=SST, body={"reason": f"qa 4.8 post {RUN}"})
        res48["pre"] = pre["status"]
        res48["post"] = post["status"]
        res48["post_body"] = post["json"]
        res48["aid"] = aid
    except Exception as exc:
        res48["err"] = str(exc)[:200]

t1 = threading.Thread(target=probe_159)
t2 = threading.Thread(target=probe_48)
t1.start(); t2.start()
t1.join(); t2.join()

R.ok("15.9", "elapsed" in res15 and 290 <= res15.get("elapsed", 0) <= 325,
     f"idle-close at {res15.get('elapsed')}s (nginx proxy_read_timeout 300s); err={res15.get('err')}")
R.ok("4.8", res48.get("pre") in (200, 400, 404) and res48.get("post") == 403 and contains(res48.get("post_body"), "no active support session"),
     f"pre={res48.get('pre')} post={res48.get('post')} {str(res48.get('post_body'))[:120]}; err={res48.get('err')}")

R.f.flush()
print("WAVE10B DONE", flush=True)
