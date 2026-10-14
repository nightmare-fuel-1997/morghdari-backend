"""Wave 3 — sections 5 (subscriber pipeline) and 6 (EMQX broker). Own TLS publisher
(paho): the repo simulator has no TLS flag and the broker is TLS-only."""
import json, os, re, socket, ssl, subprocess, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, contains
import paho.mqtt.client as mqtt

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
RUN = str(int(time.time()))[-6:]
ORG = "4f9ccc3e-cb08-4e0d-b52d-8c88a1496d99"   # Demo Farm (stable across reseed? no -> read below)
OTHER_ORG = "95ffd874-02bb-49c1-a165-386ef07292d4"

def psql(sql, user=None, pw=None):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", user or env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": pw or env1["POSTGRES_ADMIN_PASSWORD"]})

ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])
def djshell(py):
    return subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                           "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                          capture_output=True, text=True)

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

OWNER = tok("demo-owner", PW)
OOWNER = tok("other-owner", PW)

# org ids (authoritative, not hardcoded)
p = psql("SELECT org_id FROM organizations WHERE name='Demo Farm'")
ORG = p.stdout.strip()
po = psql("SELECT org_id FROM organizations WHERE name='Other Co'")
OTHER_ORG = po.stdout.strip()

CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub_client(user, pw):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA)
    c.username_pw_set(user, pw)
    c.connect("localhost", 8883, 60)
    c.loop_start()
    time.sleep(0.6)
    return c

def pub1(user, pw, topic, payload, qos=1, timeout=8):
    c = pub_client(user, pw)
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload)
    if isinstance(payload, str):
        payload = payload.encode()
    info = c.publish(topic, payload, qos=qos)
    try:
        info.wait_for_publish(timeout=timeout)
        ok = info.is_published()
    except Exception:
        ok = False
    time.sleep(0.4)
    c.loop_stop(); c.disconnect()
    return ok

def ts_now(delta_s=0):
    return (datetime.now(timezone.utc).timestamp() + delta_s)
def iso(ts=None):
    return datetime.fromtimestamp(ts or ts_now(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def good_payload(slot, hw, pack="P-A", temp=26.5, nh3=12.0, dts=None, buffered=False, schema="2.0", extra=None):
    d = {"gateway_hardware_id": hw, "gateway_slot_id": slot, "schema_version": schema,
         "device_ts": dts or iso(), "is_buffered": buffered, "clock_drift": False,
         "readings": [{"pack_id": pack, "metrics": {"temp": temp, "humidity": 58.0, "nh3": nh3, "lux": 300.0}}]}
    if extra:
        d.update(extra)
    return d

T1 = f"{ORG}/farms/1/gateways/GW-QA0001/telemetry"
def db_count(): return int(psql("SELECT count(*) FROM saloon_telemetry").stdout.strip())
def xlen(s): return int(redis("XLEN", s) or 0)
def last_dlq_reason():
    out = redis("XREVRANGE", "telemetry:dead_letter", "+", "-", "COUNT", "1")
    m = re.search(r"reason\s*\n([^\n]+)", out)
    return (m.group(1).strip() if m else out[-120:])

# ---------- fixtures ----------
djshell("""
import bcrypt
from apps.devices.models import GatewaySlot, GatewayHardwareRegistry, GatewaySensorMapping, SaloonSensorSlot
from apps.mqtt_auth.models import MqttDeviceCredential, MqttAclRule
from apps.tenants.models import Organization
from apps.farms.models import Farm
demo = Organization.objects.get(name='Demo Farm'); farm1 = Farm.objects.get(id=1)
MqttDeviceCredential.objects.update_or_create(device_id='GW-QA0001', defaults={'username':'qa-pub','password_hash':bcrypt.hashpw(b'qa-pub-pw-123',bcrypt.gensalt()).decode(),'is_active':True})
MqttAclRule.objects.get_or_create(username='qa-pub', topic='#', action='publish', defaults={'permission':'allow'})
slot,_ = GatewaySlot.objects.get_or_create(slot_id='GW-QAREJ', defaults={'org':demo,'farm':farm1,'provisioning_status':'REJECTED'})
GatewaySlot.objects.filter(pk=slot.pk).update(provisioning_status='REJECTED', org=demo, farm=farm1)
GatewayHardwareRegistry.objects.update_or_create(hardware_id='HWM-QAREJ', org=demo, removed_at=None, defaults={'slot':slot})
ss = SaloonSensorSlot.objects.get(pk='SS-00010')
GatewaySensorMapping.objects.update_or_create(gateway_hardware_id='HWM-QAREJ', pack_id='P-A', defaults={'slot':ss,'org':demo})
for hw in ('HWM-QA04','HWM-QA05'):
    GatewaySensorMapping.objects.update_or_create(gateway_hardware_id=hw, pack_id='P-A', defaults={'slot':ss,'org':demo})
MqttDeviceCredential.objects.update_or_create(device_id='GW-QASUSP', defaults={'username':'GW-QASUSP','password_hash':bcrypt.hashpw(b'susp-pw-1',bcrypt.gensalt()).decode(),'is_active':True})
MqttDeviceCredential.objects.update_or_create(device_id='GW-QAPEND', defaults={'username':'qa-inact','password_hash':bcrypt.hashpw(b'inact-pw-1',bcrypt.gensalt()).decode(),'is_active':False})
GatewaySensorMapping.objects.update_or_create(gateway_hardware_id='HWM-LEGACY', pack_id='P-A', defaults={'slot':ss,'org':demo})
print('fixtures-ok')
""")
QAPW = "qa-pub-pw-123"

def wait_for(fn, timeout=25, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(1.0)
    R.v("WAIT", "INFO", f"timeout waiting: {what}")
    return False

# ---------- 6.1 plaintext refused ----------
s = socket.socket(); s.settimeout(5)
try:
    s.connect(("127.0.0.1", 1883)); refused = False
except (ConnectionRefusedError, OSError):
    refused = True
finally:
    s.close()
R.ok("6.1", refused, "1883 refused" if refused else "1883 ACCEPTED (unexpected)")

# ---------- 6.2 / 6.3 / 6.4 / 6.5 connects ----------
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
        got["rc"] = f"ERR {e}"
    return got.get("rc")
R.ok("6.2", try_connect("GW-QA0001", "GW-QA0001-mqtt-pw") == 0, "gateway TLS connect accepted")
R.ok("6.3", try_connect("GW-QA0001", "wrong-pw") in (4, 5, "ERR [Errno 104] Connection reset by peer"), "bad password rejected")
R.ok("6.4", try_connect("GW-QASUSP", "susp-pw-1") in (4, 5), "SUSPENDED slot credential rejected by authn SQL")
R.ok("6.5", try_connect("qa-inact", "inact-pw-1") in (4, 5), "is_active=false rejected")

# ---------- 5.1 valid ingest ----------
c0, x0, d0 = db_count(), xlen("telemetry:realtime"), xlen("telemetry:dead_letter")
dts1 = iso()
ok = pub1("qa-pub", QAPW, T1, good_payload("GW-QA0001", "HWM-QA01", dts=dts1))
R.ok("5.1a", ok, "publish acked")
got = wait_for(lambda: db_count() > c0, what="db row for valid T1")
R.ok("5.1b", got and xlen("telemetry:realtime") > x0 and xlen("telemetry:dead_letter") == d0,
     f"db {c0}->{db_count()} rt {x0}->{xlen('telemetry:realtime')} dlq={xlen('telemetry:dead_letter')}")
dk = redis("EXISTS", f"dedup:{ORG}:SS-00010:{dts1}")
R.ok("5.1c", dk == "1", f"dedup key present={dk}")

# ---------- 5.12 duplicate dropped ----------
x1 = xlen("telemetry:realtime"); c1 = db_count()
pub1("qa-pub", QAPW, T1, good_payload("GW-QA0001", "HWM-QA01", dts=dts1))
time.sleep(6)
R.ok("5.12", xlen("telemetry:realtime") == x1 and db_count() == c1 and xlen("telemetry:dead_letter") == d0,
     f"rt={xlen('telemetry:realtime')} db={db_count()} dlq={xlen('telemetry:dead_letter')} (unchanged)")

# ---------- DLQ reasons ----------
def dlq_case(tid, topic, payload, expect):
    before = xlen("telemetry:dead_letter"); c = db_count()
    pub1("qa-pub", QAPW, topic, payload)
    ok = wait_for(lambda: xlen("telemetry:dead_letter") > before, what=tid)
    R.ok(tid, ok and last_dlq_reason() == expect and db_count() == c, f"reason={last_dlq_reason()!r}")

dlq_case("5.3", T1, "hello", "bad_json")
dlq_case("5.4", T1, [1, 2, 3], "bad_json")
dlq_case("5.5", T1, good_payload("GW-QA0001X", "HWM-QA01"), "TOPIC_PAYLOAD_IDENTITY_MISMATCH")
dlq_case("5.5b", T1, {k: v for k, v in good_payload("GW-QA0001", "HWM-QA01").items() if k != "gateway_slot_id"}, "TOPIC_PAYLOAD_IDENTITY_MISMATCH")
dlq_case("5.6", T1, good_payload("GW-QA0001", "HWM-QA01", schema="1.9"), "UNSUPPORTED_SCHEMA_VERSION")
p7 = good_payload("GW-QA0001", "HWM-QA01"); del p7["device_ts"]
dlq_case("5.7", T1, p7, "missing_device_ts")
dlq_case("5.8", f"{ORG}/farms/1/gateways/GW-NOPE/telemetry", good_payload("GW-NOPE", "HWM-NOPE"), "UNKNOWN_SLOT")
# 5.2: reachable bad_topic = status-shaped topic (subscriber IS subscribed to /status but pipeline only knows T1)
dlq_case("5.2", f"{ORG}/farms/1/gateways/GW-QA0001/status",
         {"status": "offline", "schema_version": "2.0", "gateway_hardware_id": "HWM-QA01",
          "gateway_slot_id": "GW-QA0001", "pack_id": "P-A", "ts": iso()}, "bad_topic")
lwt = redis("KEYS", "sensor:lwt:*")
R.ok("5.2b", lwt == "", f"sensor:lwt keys after status msg: {lwt!r} (handler unwired -> decision note)")

# ---------- 5.9 / 5.10 / 5.11 ----------
TS = f"{ORG}/farms/1/gateways/GW-QASUSP/telemetry"
dlq_case("5.9a", TS, good_payload("GW-QASUSP", "HWM-QA04"), "gateway_suspended")
TR = f"{ORG}/farms/1/gateways/GW-QAREJ/telemetry"
dlq_case("5.9b", TR, good_payload("GW-QAREJ", "HWM-QAREJ"), "gateway_rejected")
xp = xlen("telemetry:pending_approval"); dp = xlen("telemetry:dead_letter")
TP = f"{ORG}/farms/1/gateways/GW-QAPEND/telemetry"
pub1("qa-pub", QAPW, TP, good_payload("GW-QAPEND", "HWM-QA05"))
ok = wait_for(lambda: xlen("telemetry:pending_approval") > xp, what="5.10 pending")
R.ok("5.10", ok and xlen("telemetry:dead_letter") == dp, f"pending {xp}->{xlen('telemetry:pending_approval')} dlq held at {dp}")
TL = f"{ORG}/farms/1/gateways/GW-LEGACY/telemetry"
cL = db_count()
pub1("qa-pub", QAPW, TL, good_payload("GW-LEGACY", "HWM-LEGACY"))
R.ok("5.11", wait_for(lambda: db_count() > cL, what="5.11 legacy flows"), "no provisioning row -> accepted")

# ---------- 5.13 / 5.14 / 5.15 ----------
cN = db_count()
pub1("qa-pub", QAPW, T1, good_payload("GW-QA0001", "HWM-QA01", temp=-999.0))
R.ok("5.13", wait_for(lambda: db_count() > cN, what="5.13 negative accepted"), "no negative-value gate")
cF = db_count()
pub1("qa-pub", QAPW, T1, good_payload("GW-QA0001", "HWM-QA01", dts=iso(ts_now(3600))))
R.ok("5.14", wait_for(lambda: db_count() > cF, what="5.14 future ts accepted"), "clock_drift passthrough, no rejection")
cM = db_count(); xM = xlen("telemetry:realtime")
t_a = iso(ts_now(7200)); t_b = datetime.fromtimestamp(ts_now(7200) + 0.000001, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
pub1("qa-pub", QAPW, T1, good_payload("GW-QA0001", "HWM-QA01", dts=t_a))
pub1("qa-pub", QAPW, T1, good_payload("GW-QA0001", "HWM-QA01", dts=t_b))
ok = wait_for(lambda: db_count() >= cM + 2 and xlen("telemetry:realtime") >= xM + 2, what="5.15 both rows")
R.ok("5.15", ok, f"db {cM}->{db_count()} (recv_ts nudged, no XADD clash)")

# ---------- 6.6 / 6.7 ACL drops ----------
cA, dA = db_count(), xlen("telemetry:dead_letter")
foreign = f"{OTHER_ORG}/farms/3/gateways/GW-QA0003/telemetry"
ok = pub1("GW-QA0001", "GW-QA0001-mqtt-pw", foreign, good_payload("GW-QA0001", "HWM-QA01"))
time.sleep(6)
R.ok("6.6", db_count() == cA and xlen("telemetry:dead_letter") == dA, f"client-ok={ok} db/db+dlq unchanged (deny_action=ignore)")
ok = pub1(env1["SUBSCRIBER_USERNAME"], env1["SUBSCRIBER_PASSWORD"], T1, good_payload("GW-QA0001", "HWM-QA01"))
time.sleep(6)
R.ok("6.7", db_count() == cA and xlen("telemetry:dead_letter") == dA, f"subscriber publish dropped, client-ok={ok}")

# ---------- 6.9 rotation on GW-QA0003 ----------
r = api("POST", "/gateways/GW-QA0003/rotate-credential/", token=OOWNER, body={})
newpw = (r["json"] or {}).get("new_password") or (r["json"] or {}).get("password")
R.ok("6.9a", r["status"] in (200, 201) and bool(newpw), {"status": r["status"], "keys": sorted((r["json"] or {}).keys())})
if newpw:
    R.ok("6.9b", try_connect("GW-QA0003", "GW-QA0003-mqtt-pw") == 0, "old password still works pre-ACK")
    ra = api("POST", "/gateways/GW-QA0003/rotation-ack/", token=OOWNER, body={"password": newpw})
    R.ok("6.9c", ra["status"] in (200, 201), {"status": ra["status"], "json": ra["json"]})
    R.ok("6.9d", try_connect("GW-QA0003", newpw) == 0 and try_connect("GW-QA0003", "GW-QA0003-mqtt-pw") in (4, 5),
         "new works, old rejected post-ACK")
    djshell("import bcrypt; from apps.mqtt_auth.models import MqttDeviceCredential;"
            "c=MqttDeviceCredential.objects.get(username='GW-QA0003');"
            "c.password_hash=bcrypt.hashpw(b'GW-QA0003-mqtt-pw',bcrypt.gensalt()).decode(); c.pending_password_hash=None; c.pending_since=None; c.save(); print('restored')")
else:
    for t in ("6.9b", "6.9c", "6.9d"): R.v(t, "NA", "no new password issued")

# ---------- 6.10 stale rotation probe ----------
d = djshell("""
import logging, io
from datetime import timedelta
from django.utils import timezone
import bcrypt
from apps.devices.models import GatewaySlot
from apps.mqtt_auth.models import MqttDeviceCredential
from apps.mqtt_auth.services import alert_stale_rotations
from apps.tenants.models import Organization
from apps.farms.models import Farm
demo = Organization.objects.get(name='Demo Farm')
slot,_ = GatewaySlot.objects.get_or_create(slot_id='GW-QASTALE', defaults={'org':demo,'farm':Farm.objects.get(id=1),'provisioning_status':'PROVISIONED'})
c,_ = MqttDeviceCredential.objects.get_or_create(device_id='GW-QASTALE', defaults={'username':'qa-stale','password_hash':bcrypt.hashpw(b'x',bcrypt.gensalt()).decode(),'is_active':True})
c.username='qa-stale'; c.pending_password_hash=bcrypt.hashpw(b'y',bcrypt.gensalt()).decode(); c.pending_since=timezone.now()-timedelta(hours=25); c.save()
buf=io.StringIO(); h=logging.StreamHandler(buf); logging.getLogger().addHandler(h)
alert_stale_rotations()
logging.getLogger().removeHandler(h)
print('LOGDUMP:'+buf.getvalue()[-300:])
c.delete(); slot.delete()
""")
R.ok("6.10", "M5-08" in d.stdout, d.stdout.strip().splitlines()[-1][:150] if d.stdout.strip() else d.stderr[-150:])

# ---------- 6.8 live_verify (plaintext dev script vs TLS-only broker) ----------
lv = subprocess.run(["python", "scripts/live_verify_emqx.py"], capture_output=True, text=True, timeout=90,
                    cwd=os.path.join(HERE, ".."), env={**os.environ, "EMQX_MQTT_PORT": "1883"})
R.ok("6.8", lv.returncode == 0, f"rc={lv.returncode} tail={(lv.stdout+lv.stderr)[-160:]!r}")

# ---------- 5.17 oversize ----------
dB = xlen("telemetry:dead_letter")
big = pub1("qa-pub", QAPW, T1, {"pad": "x" * (1200 * 1024)}, timeout=6)
R.ok("5.17", xlen("telemetry:dead_letter") == dB, f"client publish ok={big}; broker-side >1MB dropped, no DLQ")

# ---------- 5.16 subscriber outage ----------
subprocess.run(["docker", "stop", "morghdari-subscriber-01", "morghdari-subscriber-02"], capture_output=True)
time.sleep(2)
cS = db_count()
for i in range(5):
    pub1("qa-pub", QAPW, T1, good_payload("GW-QA0001", "HWM-QA01"))
subprocess.run(["docker", "start", "morghdari-subscriber-01", "morghdari-subscriber-02"], capture_output=True)
R.ok("5.16", wait_for(lambda: db_count() >= cS + 5, timeout=60, what="5.16 catch-up"), f"db {cS}->{db_count()} after restart (QoS1 backlog)")

# ---------- 5.18 SUBACK denied (LAST; rules restored after) ----------
bak = psql("SELECT username,topic,permission,action FROM mqtt_acl_rules WHERE username IN ('subscriber-01','subscriber-02')").stdout
psql("DELETE FROM mqtt_acl_rules WHERE username IN ('subscriber-01','subscriber-02')")
time.sleep(12)
logs = subprocess.run(["docker", "logs", "morghdari-subscriber-01", "--since", "2m"], capture_output=True, text=True).stdout
denied = "SUBACK DENIED" in logs and "resubscribing in 5s" in logs
stats = subprocess.run(["docker", "exec", "morghdari-daphne", "curl", "-s", "http://morghdari-subscriber-01:9091/metrics"],
                       capture_output=True, text=True).stdout
for row in [l for l in bak.strip().splitlines() if l.strip()]:
    u, t, p_, a = [x.strip() for x in row.split("|")]
    psql(f"INSERT INTO mqtt_acl_rules (username,topic,permission,action) VALUES ('{u}','{t}','{p_}','{a}')")
time.sleep(12)
logs2 = subprocess.run(["docker", "logs", "morghdari-subscriber-01", "--since", "1m"], capture_output=True, text=True).stdout
R.ok("5.18", denied, f"denied+retry logged={denied}; resubscribed={'subscribed' in logs2.lower()}")
R.f.flush()
print("\nWAVE3 DONE")
