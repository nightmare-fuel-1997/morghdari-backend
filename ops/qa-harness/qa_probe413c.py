"""Probe 4.13c: full populate -> dispatch -> verify drop (db1, prefixed key)."""
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
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = {}
for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8"):
    m = re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())
    if m:
        env1[m.group(1)] = m.group(2)

PW = "password12345"
MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")

def totp(secret):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int(time.time() // 30))
    h = hmac.new(k, msg, hashlib.sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

OWNER = tok("demo-owner", PW)
ORG = claims(OWNER)["org_id"]
ST = api("POST", "/auth/login/", body={"username": "support1", "password": PW,
        "mfa_code": totp(MFA_SECRET)})["json"]["access_token"]
SST = api("POST", "/platform/support/session/", token=ST,
          body={"org_id": ORG, "incident_id": "QA-PROBE413C"})["json"]["access_token"]
KEY = ":1:location:tree:" + ORG
assert api("GET", "/provisioning/location-tree/", token=OWNER)["status"] == 200
k1 = redis("-n", "1", "EXISTS", KEY)
r = api("POST", "/platform/support/tasks/drain/", token=SST, body={"task": "rebuild_zone_cache", "reason": "qa413"})
k2 = redis("-n", "1", "EXISTS", KEY)
R.ok("4.13", r["status"] == 202 and k1 == "1" and k2 == "0", f"dispatch={r['status']} key_before={k1} key_after={k2}")
R.f.flush()
print("PROBE413C DONE", flush=True)
