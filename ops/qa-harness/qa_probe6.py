"""Probe: sensor register shape + suspend matrix + review perm."""
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
from qa_lib import api, tok

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

ST = api("POST", "/auth/login/", body={"username": "support1", "password": PW,
        "mfa_code": totp(MFA_SECRET)})["json"]["access_token"]

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()

ORG = psql("SELECT org_id FROM organizations WHERE name='Demo Farm'")
SST = api("POST", "/platform/support/session/", token=ST,
          body={"org_id": ORG, "incident_id": "QA-PROBE"})["json"]["access_token"]

for body in ({"slot_id": "GW-QAHW2", "hardware_id": "HW-QA-B", "reason": "qa"},
             {"gateway_slot_id": "GW-QAHW2", "hardware_id": "HW-QA-B", "reason": "qa"},
             {"slot_id": "GW-QAHW2", "sensor_hardware_id": "HW-QA-B", "reason": "qa"}):
    r = api("POST", "/platform/support/hardware/sensors/", token=SST, body=body)
    print("sensor-reg", sorted(body)[:2], "->", r["status"], r["json"], flush=True)

for slot, action in [("GW-QAREJ", "suspend"), ("GW-QAPEND", "suspend"), ("GW-QA0001", "resume"),
                     ("GW-QAPEND", "resume")]:
    r = api("POST", f"/platform/support/gateways/{slot}/provisioning/", token=SST, body={"action": action})
    print(f"{slot} {action} ->", r["status"], r["json"], flush=True)

MGR = tok("manager1", PW)
r = api("POST", "/gateway-requests/1/review/", token=MGR, body={"decision": "MAYBE"})
print("mgr review ->", r["status"], r["json"], flush=True)
