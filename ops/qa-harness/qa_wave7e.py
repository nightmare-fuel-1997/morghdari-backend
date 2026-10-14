"""Wave 7e — 20.4 support lifecycle incl. real 900s expiry."""
import base64
import hashlib
import hmac
import os
import re
import struct
import subprocess
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec

R = Rec()
PW = "password12345"
MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")

def totp(secret):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int(time.time() // 30))
    h = hmac.new(k, msg, __import__("hashlib").sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

r = api("POST", "/auth/login/", body={"username": "support1", "password": PW, "mfa_code": totp(MFA_SECRET)})
assert r["status"] == 200, r["json"]
ST = r["json"]["access_token"]
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
ORG = subprocess.run(["docker", "exec", "morghdari-postgres", "psql",
    "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
    "-c", "SELECT org_id FROM organizations WHERE name='Demo Farm'"],
    capture_output=True, text=True,
    env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()
r = api("POST", "/platform/support/session/", token=ST,
        body={"org_id": ORG, "incident_id": "QA-E2E204"})
assert r["status"] == 200, r["json"]
SST = r["json"]["access_token"]
c = claims(SST)
R.ok("20.4a", c.get("support_session") is True and c.get("exp", 0) - c.get("iat", 1) == 900,
     f"support_session={c.get('support_session')} lifetime={c.get('exp', 0) - c.get('iat', 1)}s")
r = api("GET", "/farms/", token=SST)
lst = r["json"] if isinstance(r["json"], list) else (r["json"] or {}).get("results", [])
R.ok("20.4b", r["status"] == 200 and len(lst) == 2, f"session reads its org ({len(lst)} farms)")
wait_s = max(0, c.get("exp", 0) - time.time() + 15)
print(f"waiting {wait_s:.0f}s for session expiry...", flush=True)
time.sleep(wait_s)
r = api("GET", "/farms/", token=SST)
R.ok("20.4c", r["status"] in (401, 403), f"post-expiry op -> {r['status']} (expected 401/403)")
R.f.flush()
print("WAVE7E DONE", flush=True)
