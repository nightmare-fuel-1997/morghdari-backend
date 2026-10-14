"""Wave 6e — section 12 leftovers: clean 12.2, corrected 12.7, true 12.11/12.12."""
import base64
import hashlib
import hmac
import struct
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.request
import urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, CTX

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")

def totp(secret):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int(time.time() // 30))
    h = hmac.new(k, msg, __import__("hashlib").sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
ORG = OW["org_id"]
MGR = tok("manager1", PW)
ST = api("POST", "/auth/login/", body={"username": "support1", "password": PW,
        "mfa_code": totp(MFA_SECRET)})["json"]["access_token"]
SST = api("POST", "/platform/support/session/", token=ST,
          body={"org_id": ORG, "incident_id": "QA-W6E"})["json"]["access_token"]
for row in psql("SELECT id FROM gateway_requests WHERE status='PENDING'").stdout.strip().splitlines():
    if row.strip():
        api("POST", f"/gateway-requests/{row.strip()}/review/", token=SST,
            body={"decision": "REJECT", "reason": "qa cleanup"})
assert psql("SELECT count(*) FROM gateway_requests WHERE status='PENDING'").stdout.strip() == "0"

ids = []
for i in range(5):
    r = api("POST", "/gateway-requests/", token=MGR, body={"farm_id": 1, "reason": f"qa-e-{i}"})
    ids.append(r["status"])
r = api("POST", "/gateway-requests/", token=MGR, body={"farm_id": 1, "reason": "qa-e-6th"})
R.ok("12.2", ids == [201] * 5 and r["status"] == 400 and (r["json"] or {}).get("error") == "pending gateway request cap reached",
     f"first5={ids} sixth={r['status']}")
for row in psql("SELECT id FROM gateway_requests WHERE status='PENDING'").stdout.strip().splitlines():
    if row.strip():
        api("POST", f"/gateway-requests/{row.strip()}/review/", token=SST,
            body={"decision": "REJECT", "reason": "qa cleanup"})

def basic(u, p):
    t = base64.b64encode(f"{u}:{p}".encode()).decode()
    req = urllib.request.Request("https://localhost/api/v1/gateways/GW-QA0001/config/",
                                 headers={"Authorization": "Basic " + t})
    try:
        resp = urllib.request.urlopen(req, timeout=20, context=CTX)
        return resp.status, resp.read()[:160].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:160].decode("utf-8", "replace")

code, body = basic("GW-QA0001", "wrong")
R.ok("12.7a", code == 404 and "gateway not found" in body, f"bad basic -> {code} (BUG-11)")
code, body = basic("GW-QA0001", "GW-QA0001-mqtt-pw")
R.ok("12.7b", code == 404 and "gateway not found" in body, f"valid device basic -> {code} (BUG-11)")
r = api("GET", "/gateways/GW-QA0001/config/", token=MGR)
R.ok("12.7c", r["status"] == 200 and (r["json"] or {}).get("gateway_slot_id") == "GW-QA0001", "manager JWT path works")

INST = tok("installer1", PW)
r = api("POST", "/provisioning/sync/", token=INST,
        body={"gateway_slot_id": "GW-QAHW1", "sensor_slot_mappings": [{}]})
R.ok("12.11a", r["status"] == 400 and (r["json"] or {}).get("error") == "mapping requires pack_id and slot_id", r["json"])
r = api("POST", "/provisioning/sync/", token=INST,
        body={"gateway_slot_id": "GW-QAHW1", "sensor_slot_mappings": [{"pack_id": "P-X", "slot_id": "SS-NOPE"}]})
R.ok("12.11b", r["status"] == 400 and (r["json"] or {}).get("error") == "sensor slot not found in caller organization", r["json"])
r = api("POST", "/provisioning/sync/", token=INST,
        body={"gateway_slot_id": "GW-QAHW1", "replacements": [{"old_hardware_id": "x"}]})
R.ok("12.12a", r["status"] == 400 and (r["json"] or {}).get("error") == "replacement requires old_hardware_id, new_hardware_id, pack_id and slot_id", r["json"])
r = api("POST", "/provisioning/sync/", token=INST,
        body={"gateway_slot_id": "GW-QAHW1", "replacements": [{"old_hardware_id": "HW-NOPE", "new_hardware_id": "HW-NOPE2", "pack_id": "P-A", "slot_id": "SS-00010"}]})
R.ok("12.12b", r["status"] == 400 and (r["json"] or {}).get("error") == "replacement references unknown hardware or sensor slot", r["json"])
st = psql("SELECT provisioning_status FROM gateway_slots WHERE slot_id='GW-QA0001'").stdout.strip()
R.ok("12.0guard", st == "PROVISIONED", f"QA0001 status={st}")
R.f.flush()
print("WAVE6E DONE", flush=True)
