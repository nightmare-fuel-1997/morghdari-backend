"""One-off: reject orphan qa-21.9 gateway requests left by the interrupted run."""
import base64, hashlib, hmac, os, re, struct, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims

PW = "password12345"
HERE = os.path.dirname(os.path.abspath(__file__))
MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")

def totp(secret, when=None):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int((when if when is not None else time.time()) // 30))
    h = hmac.new(k, msg, hashlib.sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

OWNER = tok("demo-owner", PW); ORG = claims(OWNER)["org_id"]
r = api("GET", "/gateway-requests/", token=tok("manager1", PW))
print("list:", r["status"], str(r["json"])[:400])
items = r["json"] if isinstance(r["json"], list) else (r["json"] or {}).get("results", [])
qa = [g for g in items if isinstance(g, dict) and "21.9-" in str(g.get("details", ""))]
print("orphans:", [(g.get("id"), g.get("status")) for g in qa])
r = api("POST", "/auth/login/", body={"username": "support1", "password": PW, "mfa_code": totp(MFA_SECRET)})
assert r["status"] == 200, r
ST = r["json"]["access_token"]
r = api("POST", "/platform/support/session/", token=ST, body={"org_id": ORG, "incident_id": "QA-CLEANUP-219"})
assert r["status"] == 200, r["json"]
SST = r["json"]["access_token"]
for g in qa:
    rr = api("POST", f"/gateway-requests/{g['id']}/review/", token=SST, body={"decision": "REJECT"})
    print("reject", g["id"], rr["status"], rr["json"])
r = api("GET", "/gateway-requests/", token=tok("manager1", PW))
items2 = r["json"] if isinstance(r["json"], list) else (r["json"] or {}).get("results", [])
pend = [g for g in items2 if isinstance(g, dict) and g.get("status") == "PENDING"]
print("pending-now:", [(g.get("id"), str(g.get("details", ""))[:40]) for g in pend])
