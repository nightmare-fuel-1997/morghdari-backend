"""Wave 3b — redo the wave-3 fixtures/tests that were invalid:
- GW-QA0001 credential was clobbered by the qa-pub fixture (one row per device_id):
  restore it, give qa-pub its own slot, re-run 6.2-6.6.
- rotation-ack takes {"new_password"} (not {"password"}): redo 6.9c/d.
- 5.18 SUBACK-denied needs a (re)subscribe: stop sub-01, drop its rules,
  boot it, observe denial+retry, restore, reboot clean.
"""
import os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
ORG = subprocess.run(["docker", "exec", "morghdari-postgres", "psql", "-U", env1["POSTGRES_ADMIN_USER"],
    "-d", env1["DATABASE_NAME"], "-w", "-t", "-A", "-c", "SELECT org_id FROM organizations WHERE name='Demo Farm'"],
    capture_output=True, text=True,
    env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])
def djshell(py):
    return subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                           "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                          capture_output=True, text=True)

print(djshell("""
import bcrypt
from apps.devices.models import GatewaySlot
from apps.mqtt_auth.models import MqttDeviceCredential, MqttAclRule
from apps.tenants.models import Organization
from apps.farms.models import Farm
demo = Organization.objects.get(name='Demo Farm')
MqttDeviceCredential.objects.update_or_create(device_id='GW-QA0001', defaults={'username':'GW-QA0001','password_hash':bcrypt.hashpw(b'GW-QA0001-mqtt-pw',bcrypt.gensalt()).decode(),'is_active':True})
slot,_ = GatewaySlot.objects.get_or_create(slot_id='GW-QAPUB', defaults={'org':demo,'farm':Farm.objects.get(id=1),'provisioning_status':'PROVISIONED'})
MqttDeviceCredential.objects.update_or_create(device_id='GW-QAPUB', defaults={'username':'qa-pub','password_hash':bcrypt.hashpw(b'qa-pub-pw-123',bcrypt.gensalt()).decode(),'is_active':True})
MqttAclRule.objects.get_or_create(username='qa-pub', topic='#', action='publish', defaults={'permission':'allow'})
print('creds-fixed')
""").stdout.strip().splitlines()[-1])

import paho.mqtt.client as mqtt
CA = os.path.join(HERE, "..", "certs", "ca.crt")
def try_connect(user, pw):
    got = {}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA)
    c.username_pw_set(user, pw)
    def on_conn(cli, ud, fl, rc, pr=None): got["rc"] = rc.value if hasattr(rc, "value") else rc
    c.on_connect = on_conn
    try:
        c.connect("localhost", 8883, 10); c.loop_start()
        t0 = time.time()
        while "rc" not in got and time.time() - t0 < 8: time.sleep(0.2)
        c.loop_stop(); c.disconnect()
    except Exception as e:
        got["rc"] = f"ERR {type(e).__name__}"
    return got.get("rc")

R.ok("6.2", try_connect("GW-QA0001", "GW-QA0001-mqtt-pw") == 0, "gateway TLS connect accepted")
R.ok("6.3", try_connect("GW-QA0001", "wrong-pw") in (4, 5), "bad password rejected")
R.ok("6.4", try_connect("GW-QASUSP", "susp-pw-1") in (4, 5), "SUSPENDED denied by authn SQL")
R.ok("6.5", try_connect("qa-inact", "inact-pw-1") in (4, 5), "is_active=false denied")

def db_count():
    return int(psql("SELECT count(*) FROM saloon_telemetry").stdout.strip())
def xlen(s):
    return int(subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
        "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"], "XLEN", s],
        capture_output=True, text=True, env={**os.environ}).stdout.strip())

# 6.6 redo with working credential: foreign publish dropped, broker silent
import json
from datetime import datetime, timezone
cA, dA = db_count(), xlen("telemetry:dead_letter")
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2); c.tls_set(ca_certs=CA)
c.username_pw_set("GW-QA0001", "GW-QA0001-mqtt-pw"); c.connect("localhost", 8883, 10); c.loop_start()
time.sleep(1.0)
info = c.publish("95ffd874-02bb-49c1-a165-386ef07292d4/farms/3/gateways/GW-QA0003/telemetry",
                 json.dumps({"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001",
                             "schema_version": "2.0", "device_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                             "readings": [{"pack_id": "P-A", "metrics": {"temp": 1}}]}), qos=1)
try: info.wait_for_publish(timeout=8); okp = info.is_published()
except Exception: okp = False
time.sleep(6); c.loop_stop(); c.disconnect()
R.ok("6.6", db_count() == cA and xlen("telemetry:dead_letter") == dA, f"client-pub-acked={okp}, db/dlq unchanged (deny_action=ignore)")

# 6.9c/d with correct key
OOWNER = tok("other-owner", "password12345")
p = psql("SELECT pending_password_hash IS NOT NULL FROM mqtt_device_credentials WHERE username='GW-QA0003'").stdout.strip()
R.ok("6.9b2", p == "t", "rotation still staged from wave3")
# recover the staged password: wave3 stored newpw only in that process; re-stage deterministically via API again
r = api("POST", "/gateways/GW-QA0003/rotate-credential/", token=OOWNER, body={})
newpw = (r["json"] or {}).get("password")
R.ok("6.9a2", r["status"] in (200, 201) and bool(newpw), r["status"])
ra = api("POST", "/gateways/GW-QA0003/rotation-ack/", token=OOWNER, body={"new_password": newpw})
R.ok("6.9c", ra["status"] in (200, 201) and (ra["json"] or {}).get("rotation_committed") is True, {"status": ra["status"], "json": ra["json"]})
R.ok("6.9d", try_connect("GW-QA0003", newpw) == 0 and try_connect("GW-QA0003", "GW-QA0003-mqtt-pw") in (4, 5),
     "new works, old rejected post-ACK")
djshell("import bcrypt; from apps.mqtt_auth.models import MqttDeviceCredential;"
        "c=MqttDeviceCredential.objects.get(username='GW-QA0003');"
        "c.password_hash=bcrypt.hashpw(b'GW-QA0003-mqtt-pw',bcrypt.gensalt()).decode(); c.pending_password_hash=None; c.pending_since=None; c.save(); print('restored')")

# 5.18 redo: force resubscribe with rules revoked
bak = psql("SELECT username||'|'||topic||'|'||permission||'|'||action FROM mqtt_acl_rules WHERE username='subscriber-01'").stdout
subprocess.run(["docker", "stop", "morghdari-subscriber-01"], capture_output=True)
psql("DELETE FROM mqtt_acl_rules WHERE username='subscriber-01'")
subprocess.run(["docker", "start", "morghdari-subscriber-01"], capture_output=True)
time.sleep(18)
logs = subprocess.run(["docker", "logs", "morghdari-subscriber-01", "--since", "90s"], capture_output=True, text=True).stdout
denied = "SUBACK DENIED" in logs and "resubscribing in 5s" in logs
for row in [l for l in bak.strip().splitlines() if l.strip()]:
    u, t, pm, a = [x.strip() for x in row.split("|")]
    psql(f"INSERT INTO mqtt_acl_rules (username,topic,permission,action) VALUES ('{u}','{t}','{pm}','{a}')")
n = psql("SELECT count(*) FROM mqtt_acl_rules WHERE username='subscriber-01'").stdout.strip()
subprocess.run(["docker", "restart", "morghdari-subscriber-01"], capture_output=True)
time.sleep(15)
logs2 = subprocess.run(["docker", "logs", "morghdari-subscriber-01", "--since", "60s"], capture_output=True, text=True).stdout
R.ok("5.18", denied and n == "2" and "subscribed" in logs2.lower(),
     f"denied+retry={denied} rules_restored={n} resubscribed={'subscribed' in logs2.lower()}")
R.f.flush()
print("\nWAVE3B DONE")
