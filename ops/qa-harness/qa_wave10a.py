"""Wave 10a — quick N-probes: 11.6, 12.5, 8.15, 4.6, 11.10. (8.13 done in qa_813.py; slow 15.9/4.8/16.12 in wave10b/c.)"""
import base64
import datetime as dt
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
from qa_lib import api, tok, claims, Rec, contains

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
RUN = str(int(time.time()))[-6:]
MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")
ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

def djshell(py):
    r = subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                        "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                       capture_output=True, text=True)
    assert r.returncode == 0, "shell failed: " + (r.stderr or "")[-300:]
    return r

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

def totp(secret, when=None):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int((when if when is not None else time.time()) // 30))
    h = hmac.new(k, msg, hashlib.sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

OWNER = tok("demo-owner", PW); ORG = claims(OWNER)["org_id"]
ENG = tok("engineer1", PW); INST = tok("installer1", PW)

# ---------------- 11.10 flock_age_days at Tehran midnight boundary ----------------
out = djshell(
    "import datetime as dt\n"
    "from apps.farms.api import _flock_age_days\n"
    "tehran = dt.timezone(dt.timedelta(hours=3, minutes=30))\n"
    "now = dt.datetime.now(dt.timezone.utc)\n"
    "yesterday_tehran_evening = (dt.datetime.now(tehran) - dt.timedelta(days=1)).replace(hour=23, minute=50, second=0, microsecond=0)\n"
    "start = yesterday_tehran_evening.astimezone(dt.timezone.utc)\n"
    "print('AGE', _flock_age_days(start, now))\n"
    "print('START', start.isoformat())\n"
).stdout
age = [l.split()[1] for l in out.splitlines() if l.startswith("AGE ")]
R.ok("11.10", age == ["1"], f"flock_age_days(23:50-yesterday-Tehran)={age} (fixed +03:30, no DST)")

# ---------------- 12.5 hardware-must-exist gate (sync endpoint carries the 400) ----------------
SLOT = f"GW-QA125{RUN}"
mk = djshell(
    "from apps.tenants.models import Organization\n"
    "from apps.farms.models import Farm\n"
    "from apps.devices.models import GatewaySlot\n"
    f"o = Organization.objects.get(org_id='{ORG}')\n"
    "f = Farm.objects.filter(org=o).first()\n"
    f"GatewaySlot.objects.filter(pk='{SLOT}').delete()\n"
    f"GatewaySlot.objects.create(slot_id='{SLOT}', org=o, farm=f, provisioning_status='PENDING_INSTALLATION')\n"
    "print('slot-created')"
)
r = api("POST", "/provisioning/sync/", token=INST, body={"gateway_slot_id": SLOT, "zones": [], "sensor_slot_mappings": []})
R.ok("12.5", r["status"] == 400 and contains(r["json"], "active gateway hardware not registered"),
     f"sync-without-hardware={r['status']} {r['json']}")
djshell(f"from apps.devices.models import GatewaySlot\nGatewaySlot.objects.filter(pk='{SLOT}').delete()\nprint('slot-gone')")

# ---------------- 4.6 audit row precedes the success response (DB-clock window) ----------------
r = api("POST", "/auth/login/", body={"username": "support1", "password": PW, "mfa_code": totp(MFA_SECRET)})
assert r["status"] == 200, r
ST = r["json"]["access_token"]
n_before = psql("SELECT count(*) FROM account_activity_logs WHERE action='SUPPORT_ORG_ENTER'").stdout.strip()
db_t0 = psql("SELECT now()").stdout.strip()
resp = api("POST", "/platform/support/session/", token=ST, body={"org_id": ORG, "incident_id": f"QA-46-{RUN}"})
db_t1 = psql("SELECT now()").stdout.strip()
row = psql("SELECT timestamp FROM account_activity_logs WHERE action='SUPPORT_ORG_ENTER' ORDER BY timestamp DESC LIMIT 1").stdout.strip()
n_after = psql("SELECT count(*) FROM account_activity_logs WHERE action='SUPPORT_ORG_ENTER'").stdout.strip()
in_window = row >= db_t0[:26] and row <= db_t1[:26] + "z"
R.ok("4.6a", resp["status"] == 200 and in_window and int(n_after) == int(n_before) + 1,
     f"audit ts {row} within [{db_t0} .. {db_t1}]; count {n_before}->{n_after}")
# failed opener (unknown org) writes NO audit row
n_bad0 = psql("SELECT count(*) FROM account_activity_logs WHERE action='SUPPORT_ORG_ENTER'").stdout.strip()
rb = api("POST", "/platform/support/session/", token=ST, body={"org_id": "00000000-0000-0000-0000-000000000000", "incident_id": "x"})
n_bad1 = psql("SELECT count(*) FROM account_activity_logs WHERE action='SUPPORT_ORG_ENTER'").stdout.strip()
R.ok("4.6b", rb["status"] == 400 and n_bad0 == n_bad1, f"bad-opener={rb['status']} audit-count {n_bad0}->{n_bad1}")

# ---------------- 11.6 operational-state with Redis down → degrade-to-allow ----------------
mk = djshell(
    "from apps.tenants.models import Organization\n"
    "from apps.farms.models import Farm, Saloon\n"
    f"o = Organization.objects.get(org_id='{ORG}')\n"
    "f = Farm.objects.filter(org=o, name='Farm1').first()\n"
    f"a, _ = Saloon.objects.get_or_create(org=o, farm=f, name='Saloon-QA116A-{RUN}')\n"
    f"b, _ = Saloon.objects.get_or_create(org=o, farm=f, name='Saloon-QA116B-{RUN}')\n"
    "print(a.id, b.id)"
)
S1, S2 = mk.stdout.strip().splitlines()[-1].split()
r = api("POST", f"/saloons/{S1}/operational-state/", token=ENG, body={"state": "PRE_HEATING"})
base_ok = r["status"] == 200 and contains(r["json"], "PRE_HEATING")
subprocess.run(["docker", "stop", "morghdari-redis"], capture_output=True)
try:
    r = api("POST", f"/saloons/{S2}/operational-state/", token=ENG, body={"state": "PRE_HEATING"})
    down_ok = r["status"] == 200 and contains(r["json"], "PRE_HEATING")
    down_body = f"{r['status']} {r['json']}"
finally:
    subprocess.run(["docker", "start", "morghdari-redis"], capture_output=True)
    t1 = time.time()
    while time.time() - t1 < 120:
        time.sleep(5)
        try:
            if redis("PING") == "PONG":
                break
        except Exception:
            pass
R.ok("11.6", base_ok and down_ok, f"baseline={base_ok} redis-down={down_ok} {down_body}")

# ---------------- 8.15 hourly message quota: code-verified NA (no runtime drop in this snapshot) ----------------
enforced = psql("SELECT count(*) FROM information_schema.tables WHERE table_name='tier_quota_mapping'").stdout.strip()
R.v("8.15", "NA",
    "code-verified: hourly message-quota DROP has no runtime enforcement in this snapshot — drain path only "
    "bumps the metering counter (apps/telemetry/consumers.py:335 'M7-08 MET-SAAS'); broker-level quotas "
    "deferred to the v1.0 quota engine (apps/mqtt_auth/services.py:103); quota_exempt_tier1_metrics is a "
    "config-only flag (system_config/migrations/0003). Re-run when the v1.0 quota engine lands.")

R.f.flush()
print("WAVE10A DONE", flush=True)
