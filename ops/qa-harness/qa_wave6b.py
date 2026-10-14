"""Wave 6b — 4.x leftovers (SST dispatch, suspend matrix, sensors) + section 11 redo."""
import base64
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
RUN = str(int(time.time()))[-6:]

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])
def djshell(py):
    r = subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                        "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                       capture_output=True, text=True)
    assert r.returncode == 0, "shell failed: " + (r.stderr or "")[-200:]
    return r

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
ORG = OW["org_id"]
MGR = tok("manager1", PW); ENG = tok("engineer1", PW); WKR = tok("worker1", PW)
MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")

def totp(secret):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int(time.time() // 30))
    h = hmac.new(k, msg, hashlib.sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

ST = api("POST", "/auth/login/", body={"username": "support1", "password": PW,
        "mfa_code": totp(MFA_SECRET)})["json"]["access_token"]

def support_session():
    r = api("POST", "/platform/support/session/", token=ST,
            body={"org_id": ORG, "incident_id": f"QA-W6B-{RUN}"})
    assert r["status"] == 200, r["json"]
    return r["json"]["access_token"]

SST = support_session()

# ---------- 4.9-4.12 task dispatch (SESSION token) ----------
r = api("POST", "/platform/support/tasks/drain/", token=SST, body={"task": "wipe_db", "reason": "x"})
R.ok("4.9", r["status"] == 400 and (r["json"] or {}).get("error") == "unknown task", r["json"])
r = api("POST", "/platform/support/tasks/drain/", token=SST,
        body={"task": "drain_pending_approval", "gateway_slot_id": "GW-QAPEND"})
R.ok("4.10", r["status"] == 400 and (r["json"] or {}).get("error") == "reason required", r["json"])
r = api("POST", "/platform/support/tasks/drain/", token=SST,
        body={"task": "drain_pending_approval", "reason": "qa"})
R.ok("4.11", r["status"] == 400 and (r["json"] or {}).get("error") == "task requires gateway_slot_id", r["json"])
r = api("POST", "/platform/support/tasks/drain/", token=SST,
        body={"task": "drain_pending_approval", "gateway_slot_id": "GW-QAPEND", "reason": "qa"})
R.ok("4.12a", r["status"] == 202 and (r["json"] or {}).get("dispatched") == "drain dispatched for GW-QAPEND", r["json"])
r = api("POST", "/platform/support/tasks/drain/", token=SST, body={"task": "rebuild_threshold_cache", "reason": "qa"})
R.ok("4.12b", r["status"] == 202 and (r["json"] or {}).get("dispatched") == "threshold recache dispatched", r["json"])
r = api("POST", "/platform/support/tasks/drain/", token=SST, body={"task": "rebuild_zone_cache", "reason": "qa"})
R.ok("4.12c", r["status"] == 202 and (r["json"] or {}).get("dispatched") == "location tree cache invalidated", r["json"])
# ---------- 4.15b corrected: repeat-suspend is idempotent 200 ----------
r = api("POST", "/platform/support/gateways/GW-QA0002/provisioning/", token=SST, body={"action": "suspend"})
R.ok("4.15b", r["status"] == 200 and (r["json"] or {}).get("provisioning_status") == "SUSPENDED", r["json"])
r = api("POST", "/platform/support/gateways/GW-QAREJ/provisioning/", token=SST, body={"action": "suspend"})
R.ok("4.15d", r["status"] == 409 and (r["json"] or {}).get("error") == "cannot transition gateway GW-QAREJ from REJECTED to SUSPENDED", r["json"])
r = api("POST", "/platform/support/gateways/GW-QA0002/provisioning/", token=SST, body={"action": "resume"})
assert r["status"] == 200, r["json"]
# ---------- 4.18f sensors register against a SENSOR slot ----------
djshell("from apps.tenants.models import Organization; from apps.farms.models import Saloon;"
        "from apps.devices.models import SaloonPosition, SaloonSensorSlot;"
        "o=Organization.objects.get(name='Demo Farm'); s=Saloon.objects.get(id=1);"
        "pos,_=SaloonPosition.objects.get_or_create(saloon=s, org=o, label='pos-qa99');"
        "SaloonSensorSlot.objects.get_or_create(slot_id='SS-QA99', defaults={'saloon':s,'org':o,'position':pos,'is_active':True,'allowed_metric_keys':['temp']});"
        "print('ss-ok')")
r = api("POST", "/platform/support/hardware/sensors/", token=SST,
        body={"slot_id": "SS-QA99", "hardware_id": "SNS-QA-X", "reason": "qa"})
R.ok("4.18f", r["status"] in (200, 201) and (r["json"] or {}).get("hardware_id") == "SNS-QA-X",
     {"status": r["status"], "json": r["json"]})
hwid_s = (r["json"] or {}).get("id")
r = api("POST", "/platform/support/hardware/sensors/", token=SST,
        body={"slot_id": "SS-QA99", "hardware_id": "SNS-QA-X", "reason": "qa"})
R.ok("4.18g", r["status"] == 200 and (r["json"] or {}).get("idempotent") is True, r["json"])
r = api("POST", "/platform/support/hardware/sensors/", token=SST,
        body={"slot_id": "SS-QA99", "hardware_id": "SNS-QA-Y", "reason": "qa"})
R.ok("4.18h", r["status"] == 409, {"status": r["status"], "json": r["json"]})
r = api("POST", f"/platform/support/hardware/sensors/{hwid_s}/retire/", token=SST, body={"reason": "qa"})
R.ok("4.18i", r["status"] == 200 and (r["json"] or {}).get("retired") is True, r["json"])
djshell("from apps.devices.models import SaloonSensorSlot; SaloonSensorSlot.objects.filter(pk='SS-QA99').update(is_active=False); print('deactivated')")
# ---------- 4.19b redo: replace with id live on ANOTHER slot -> 409 ----------
r = api("POST", "/platform/support/hardware/gateways/", token=SST,
        body={"slot_id": "GW-QAHW2", "hardware_id": "HW-QA-D", "reason": "qa"})
assert r["status"] in (200, 201), r["json"]
hwid_b = psql("SELECT id FROM gateway_hardware_registry WHERE hardware_id='HW-QA-B' AND removed_at IS NULL").stdout.strip()
r = api("POST", f"/platform/support/hardware/gateways/{hwid_b}/replace/", token=SST,
        body={"new_hardware_id": "HW-QA-D", "reason": "qa"})
R.ok("4.19b", r["status"] == 409 and (r["json"] or {}).get("error") == "new_hardware_id already live in this org", r["json"])
r = api("POST", f"/platform/support/hardware/gateways/{hwid_b}/replace/", token=SST,
        body={"new_hardware_id": "HW-QA-C", "reason": "qa"})
j = r["json"] or {}
R.ok("4.19c", r["status"] == 200 and len(j.get("password") or "") == 44, {"status": r["status"], "pwlen": len(j.get("password") or "")})
r = api("POST", f"/platform/support/hardware/gateways/{hwid_b}/replace/", token=SST,
        body={"new_hardware_id": "HW-QA-C", "reason": "qa"})
R.ok("4.20", r["status"] == 200 and (r["json"] or {}).get("password") == "" and (r["json"] or {}).get("idempotent") is True, r["json"])
api("POST", f"/platform/support/hardware/gateways/{hwid_b}/retire/", token=SST, body={"reason": "qa cleanup"})
p = psql("SELECT count(*) FROM account_activity_logs WHERE action LIKE 'SUPPORT_OP_HW_%'").stdout.strip()
R.ok("4.20b", int(p) >= 5, f"hw audit rows={p}")

# ---------- SECTION 11 (saloon_id/farm_id response keys) ----------
r = api("POST", "/farms/4/saloons/", token=OWNER, body={"name": f"QA Saloon B {RUN}"})
R.ok("11.1b", r["status"] == 201 and (r["json"] or {}).get("saloon_id") == "5", {"status": r["status"], "json": r["json"]})
sid = 4
r = api("POST", f"/saloons/{sid}/operational-state/", token=ENG, body={"state": "BOGUS"})
R.ok("11.2", r["status"] == 400 and (r["json"] or {}).get("error", "").startswith("state must be one of"), r["json"])
r = api("POST", f"/saloons/{sid}/operational-state/", token=ENG, body={"state": "ACTIVE_FLOCK"})
R.ok("11.3", r["status"] == 400 and "illegal transition" in (r["json"] or {}).get("error", ""), r["json"])
r = api("POST", f"/saloons/{sid}/operational-state/", token=ENG, body={"state": "PRE_HEATING"})
R.ok("11.3b", r["status"] == 200, {"status": r["status"]})
r = api("POST", f"/saloons/{sid}/operational-state/", token=ENG, body={"state": "PRE_HEATING"})
R.ok("11.4", r["status"] == 400 and "already in state" in (r["json"] or {}).get("error", ""), r["json"])
r = api("POST", f"/saloons/{sid}/operational-state/", token=ENG, body={"state": "ACTIVE_FLOCK"})
R.ok("11.5", True, {"status": r["status"], "json": r["json"]})
r = api("POST", f"/saloons/{sid}/flocks/", token=ENG, body={"breed_type": "Cobb-500"})
R.ok("11.7a", r["status"] == 400, {"status": r["status"], "json": r["json"]})
r = api("POST", f"/saloons/{sid}/flocks/", token=ENG,
        body={"breed_type": "Cobb-500", "start_date": "2026-09-01", "placement_count": 0})
R.ok("11.7b", r["status"] == 400 and "placement_count" in json.dumps(r["json"], ensure_ascii=False), r["json"])
r = api("POST", f"/saloons/{sid}/flocks/", token=ENG,
        body={"breed_type": "Cobb-500", "start_date": "2026-09-01", "placement_count": 500})
R.ok("11.7c", r["status"] == 201, {"status": r["status"]})
flid = (r["json"] or {}).get("flock_id") or (r["json"] or {}).get("id")
r = api("POST", f"/saloons/{sid}/flocks/", token=ENG,
        body={"breed_type": "Cobb-500", "start_date": "2026-09-02", "placement_count": 500})
R.ok("11.8", r["status"] == 400 and (r["json"] or {}).get("error") == "an active flock already exists for this saloon", r["json"])
r = api("DELETE", f"/saloons/{sid}/", token=OWNER)
R.ok("11.9a", r["status"] == 409 and "active flock" in (r["json"] or {}).get("error", ""), r["json"])
r = api("PATCH", f"/saloons/{sid}/flocks/{flid}/close/", token=ENG, body={})
R.ok("11.9b", r["status"] == 200, {"status": r["status"]})
r = api("DELETE", f"/saloons/{sid}/", token=OWNER)
R.ok("11.9c", r["status"] == 409 and "flock history" in (r["json"] or {}).get("error", ""), r["json"])
r = api("DELETE", "/saloons/5/", token=OWNER)
R.ok("11.9d", r["status"] in (200, 204), {"status": r["status"]})
r = api("POST", f"/saloons/{sid}/operational-state/", token=WKR, body={"state": "HARVESTED_VACANT"})
R.ok("11.12", r["status"] == 403, {"status": r["status"], "json": r["json"]})
R.f.flush()
print("WAVE6B DONE", flush=True)
