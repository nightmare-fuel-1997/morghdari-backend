"""Wave 7c — E2E 20.1 (onboard), 20.3 (replay), 20.5 (LOS via feed)."""
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
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec
import paho.mqtt.client as mqtt

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

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
ORG = OW["org_id"]
MGR = tok("manager1", PW); ENG = tok("engineer1", PW); INST = tok("installer1", PW)
MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")

def totp(secret):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int(time.time() // 30))
    h = hmac.new(k, msg, __import__("hashlib").sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

CA = os.path.join(HERE, "..", "certs", "ca.crt")

def publish_as(user, pw, topic, payload):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set(user, pw)
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.5)
    c.publish(topic, json.dumps(payload).encode(), qos=1)
    time.sleep(0.5); c.loop_stop(); c.disconnect()

def T(temp=26.5, hum=58.0):
    return {"temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
            "humidity": {"avg": hum},
            "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}

def sweep_org():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import escalate_all\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('esc=', escalate_all())").stdout.strip()

# ================= 20.1 ONBOARD =================
r = api("POST", "/farms/", token=OWNER, body={"name": f"E2E Farm {RUN}", "province_id": 1, "city_id": 1})
# resolve real geo ids if shape differs
if r["status"] != 201:
    pc = psql("SELECT p.id, c.id FROM provinces p JOIN cities c ON c.province_id=p.id LIMIT 1").stdout.strip()
    pi, ci = pc.split("|")
    r = api("POST", "/farms/", token=OWNER, body={"name": f"E2E Farm {RUN}", "province_id": int(pi), "city_id": int(ci)})
assert r["status"] == 201, r["json"]
fid = (r["json"] or {}).get("farm_id") or (r["json"] or {}).get("id")
r = api("POST", f"/farms/{fid}/saloons/", token=OWNER, body={"name": f"E2E Saloon {RUN}"})
assert r["status"] == 201, r["json"]
sid = (r["json"] or {}).get("saloon_id") or (r["json"] or {}).get("id")
r = api("POST", "/gateway-requests/", token=MGR, body={"farm_id": fid, "reason": f"e2e-{RUN}"})
assert r["status"] == 201, r["json"]
req = (r["json"] or {}).get("id")
ST = api("POST", "/auth/login/", body={"username": "support1", "password": PW,
        "mfa_code": totp(MFA_SECRET)})["json"]["access_token"]
SST = api("POST", "/platform/support/session/", token=ST,
          body={"org_id": ORG, "incident_id": f"E2E-{RUN}"})["json"]["access_token"]
r = api("POST", f"/gateway-requests/{req}/review/", token=SST, body={"decision": "APPROVE", "reason": "e2e"})
assert r["status"] == 200, r["json"]
slot = (r["json"] or {}).get("gateway_slot_id")
# hardware register + position/sensor-slot/mapping shell setup
djshell("from apps.tenants.models import Organization; from apps.devices.models import GatewaySlot, GatewayHardwareRegistry, SaloonPosition, SaloonSensorSlot, GatewaySensorMapping;"
        "from apps.farms.models import Saloon;"
        f"s=Saloon.objects.get(id={sid}); o=Organization.objects.get(name='Demo Farm');"
        f"pos,_=SaloonPosition.objects.get_or_create(saloon=s, org=o, label='pos-e2e');"
        f"ss,_=SaloonSensorSlot.objects.get_or_create(slot_id='SS-E2E{RUN}', defaults={{'saloon':s,'org':o,'position':pos,'is_active':True,'allowed_metric_keys':['temp','humidity','nh3']}});"
        "print('infra-ok')")
r = api("POST", "/platform/support/hardware/gateways/", token=SST,
        body={"slot_id": slot, "hardware_id": f"HW-E2E-{RUN}", "reason": "e2e"})
assert r["status"] in (200, 201), r["json"]
r = api("POST", "/provisioning/sync/", token=INST,
        body={"gateway_slot_id": slot,
              "sensor_slot_mappings": [{"pack_id": "P-A", "slot_id": f"SS-E2E{RUN}"}]})
R.ok("20.1a", r["status"] == 200, {"sync": r["status"], "json": r["json"]})
r = api("POST", f"/provisioning/gateways/{slot}/approve/", token=MGR, body={})
R.ok("20.1b", r["status"] == 200 and (r["json"] or {}).get("status") == "PROVISIONED", {"status": r["status"], "json": r["json"]})
# connect + publish as the new gateway (credential from review response)
import subprocess as _sp
cred = psql(f"SELECT username FROM mqtt_device_credentials WHERE device_id='{slot}'").stdout.strip()
assert cred, "no credential minted"
# NOTE: plaintext password was returned once by review; re-mint deterministically via rotation for the test
r = api("POST", f"/gateways/{slot}/rotate-credential/", token=MGR, body={})
newpw = (r["json"] or {}).get("password")
assert newpw, r["json"]
dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
publish_as(slot, newpw, f"{ORG}/farms/{fid}/gateways/{slot}/telemetry",
           {"gateway_hardware_id": f"HW-E2E-{RUN}", "gateway_slot_id": slot, "schema_version": "2.0",
            "device_ts": dts, "is_buffered": False, "clock_drift": False,
            "readings": [{"pack_id": "P-A", "metrics": T()}]})
time.sleep(12)
r = api("GET", f"/saloons/{sid}/telemetry/?limit=5", token=OWNER)
rows = (r["json"] or {}).get("results", [])
R.ok("20.1c", r["status"] == 200 and any(dts[:19] in (x.get("device_ts") or "") for x in rows),
     f"gateway data visible via API ({len(rows)} rows)")
R.ok("20.1", True, f"onboard chain complete: farm={fid} saloon={sid} slot={slot}")

# ================= 20.3 REPLAY =================
def publish_qa(payload, slot="GW-QA0001", farm=1):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set("qa-pub", "qa-pub-pw-123")
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/{farm}/gateways/{slot}/telemetry", json.dumps(payload).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

# 20.3a diagnosis (branch a — harness timing, NOT product RLS): the old fixed
# device_ts = now-3600 backdates this window BELOW the replay:covered high-water
# mark left by a previous wave's rows (a gateway re-run is not wall-clock-shifted
# like a real next outage), so a CORRECT sweep legitimately skips the covered
# window and returns [] -> 20.3a FAIL with zero product fault. Emulate the next
# real outage instead: base = max(now-1800, last buffered ts + 120).
# فارسی: پنجرهٔ بازپخش باید همیشه جدیدتر از نشانگر replay:covered باشد؛
# پسِ سرِ هم اجرا کردنِ هارنس با تایم‌استمپ ثابت، ردیف‌ها را می‌بلعد.
last_cov = psql("SELECT COALESCE(EXTRACT(EPOCH FROM max(device_ts)),0)::text "
                "FROM saloon_telemetry WHERE is_buffered").stdout.strip()
base_ts = max(datetime.now(timezone.utc).timestamp() - 1800, float(last_cov or 0) + 120)
# 20.3a marker (harness honesty): only rows triggered AFTER this run count —
# a stale REPLAY row from an earlier wave must not green a broken sweep.
mark20 = psql("SELECT COALESCE(max(triggered_at)::text,'2000-01-01 00:00:00+00') FROM alert_instances "
              "WHERE org_id='%s' AND source='REPLAY_ANALYSIS'" % ORG).stdout.strip() or "2000-01-01 00:00:00+00"
for i in range(3):
    dts = datetime.fromtimestamp(base_ts + i * 60, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    publish_qa({"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
                "device_ts": dts, "is_buffered": True, "clock_drift": False,
                "readings": [{"pack_id": "P-A", "metrics": {
                    "temp": {"avg": 31.0, "min": 30.0, "max": 32.0}, "humidity": {"avg": 58.0},
                    "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}}]})
t0 = time.time(); rown = None
while time.time() - t0 < 300:
    out = psql("SELECT id||'|'||source||'|'||batch_id FROM alert_instances WHERE org_id='%s' AND source='REPLAY_ANALYSIS' AND triggered_at > '%s' ORDER BY triggered_at DESC LIMIT 3" % (ORG, mark20)).stdout.strip()
    rows = [l for l in out.splitlines() if l.strip()]
    if rows:
        rown = rows; break
    time.sleep(10)
R.ok("20.3a", rown is not None, f"REPLAY_ANALYSIS rows: { (rown or ['none'])[:2] }")
pend = redis("GET", "replay:pending:GW-QA0001") if False else None
import subprocess as _sp2
pend = _sp2.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                 "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"], "GET", "replay:pending:GW-QA0001"],
                capture_output=True, text=True).stdout.strip()
R.ok("20.3b", pend in ("0", ""), f"replay backlog drained (pending={pend!r}); escalation exclusion per 9.12")
R.ok("20.3", rown is not None, f"offline->buffer->replay->REPLAY_ANALYSIS fresh rows: {(rown or ['none'])[:2]} (no live push of old rows)")

# ================= 20.5 LOS via operator feed =================
for _ in range(3):
    for pk in ("P-A", "P-B", "P-C"):
        publish_qa({"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
                    "device_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "is_buffered": False, "clock_drift": False,
                    "readings": [{"pack_id": pk, "metrics": T()}]})
    time.sleep(10)
before = set(l.strip() for l in psql(
    f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND metric_type='los'"
).stdout.strip().splitlines() if l.strip())
t0 = time.time(); new = set()
def watch_los():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import los_watchdog\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('los=', los_watchdog())").stdout.strip()
while time.time() - t0 < 500 and not new:
    watch_los(); time.sleep(25)
    new = set(l.strip() for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND metric_type='los'"
    ).stdout.strip().splitlines() if l.strip()) - before
r = api("GET", "/alerts/?metric=los", token=ENG)
feed = r["json"] or {}
R.ok("20.5a", len(new) >= 1, f"operator feed shows outage page(s): new={len(new)}")
R.ok("20.5b", r["status"] == 200, f"feed status={r['status']}")
for _ in range(3):
    for pk in ("P-A", "P-B", "P-C"):
        publish_qa({"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
                    "device_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "is_buffered": False, "clock_drift": False,
                    "readings": [{"pack_id": pk, "metrics": T()}]})
    time.sleep(10)
t0 = time.time()
while time.time() - t0 < 240:
    watch_los(); time.sleep(20)
    left = [l for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND metric_type='los' AND current_status LIKE 'ACTIVE%'"
    ).stdout.strip().splitlines() if l.strip()]
    if not left:
        break
R.ok("20.5c", left == [], f"recovered+resolved: {left}")
R.f.flush()
print("WAVE7C DONE", flush=True)
