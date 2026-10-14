"""Wave 3d — 6.9 rotation verified end-to-end (no restore until proven) + 5.18
SUBACK-denied with a cold broker authz cache (EMQX restart)."""
import os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, Rec

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))

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
    except Exception:
        got["rc"] = "ERR"
    return got.get("rc")

OOWNER = tok("other-owner", "password12345")
r = api("POST", "/gateways/GW-QA0003/rotate-credential/", token=OOWNER, body={})
newpw = (r["json"] or {}).get("password")
R.ok("6.9a", r["status"] in (200, 201) and bool(newpw), {"status": r["status"]})
if newpw:
    R.ok("6.9b", try_connect("GW-QA0003", "GW-QA0003-mqtt-pw") == 0, "old password still works pre-ACK")
    ra = api("POST", "/gateways/GW-QA0003/rotation-ack/", token=OOWNER, body={"new_password": newpw})
    R.ok("6.9c", (ra["json"] or {}).get("rotation_committed") is True, ra["json"])
    rn, ro = try_connect("GW-QA0003", newpw), try_connect("GW-QA0003", "GW-QA0003-mqtt-pw")
    R.ok("6.9d", rn == 0 and ro in (4, 5, 134, 135), f"new_rc={rn} old_rc={ro}")
    djshell("import bcrypt; from apps.mqtt_auth.models import MqttDeviceCredential;"
            "c=MqttDeviceCredential.objects.get(username='GW-QA0003');"
            "c.password_hash=bcrypt.hashpw(b'GW-QA0003-mqtt-pw',bcrypt.gensalt()).decode(); c.pending_password_hash=None; c.pending_since=None; c.save(); print('restored')")
    R.ok("6.9e", try_connect("GW-QA0003", "GW-QA0003-mqtt-pw") == 0, "seed password restored afterwards")

# ---------- 5.18 ----------
subprocess.run(["docker", "stop", "morghdari-subscriber-01", "morghdari-subscriber-02"], capture_output=True)
bak = psql("SELECT username||'|'||topic||'|'||permission||'|'||action FROM mqtt_acl_rules WHERE username='subscriber-01'").stdout
psql("DELETE FROM mqtt_acl_rules WHERE username='subscriber-01'")
print("rules deleted; cold-starting broker...", flush=True)
subprocess.run(["docker", "restart", "morghdari-emqx"], capture_output=True)
time.sleep(25)
subprocess.run(["docker", "start", "morghdari-subscriber-01"], capture_output=True)
time.sleep(25)
logs = subprocess.run(["docker", "logs", "morghdari-subscriber-01", "--since", "120s"], capture_output=True, text=True).stdout
denied = "SUBACK DENIED" in logs and "resubscribing in 5s" in logs
R.ok("5.18a", denied, "SUBACK DENIED + 5s retry with zero rules and cold cache")
for row in [l for l in bak.strip().splitlines() if l.strip()]:
    u, t, pm, a = [x.strip() for x in row.split("|")]
    psql(f"INSERT INTO mqtt_acl_rules (username,topic,permission,action) VALUES ('{u}','{t}','{pm}','{a}')")
n = psql("SELECT count(*) FROM mqtt_acl_rules WHERE username='subscriber-01'").stdout.strip()
subprocess.run(["docker", "restart", "morghdari-emqx"], capture_output=True)
time.sleep(25)
subprocess.run(["docker", "start", "morghdari-subscriber-01", "morghdari-subscriber-02"], capture_output=True)
time.sleep(20)
logs2 = subprocess.run(["docker", "logs", "morghdari-subscriber-01", "--since", "120s"], capture_output=True, text=True).stdout
h = subprocess.run(["docker", "inspect", "morghdari-subscriber-01", "morghdari-subscriber-02",
                    "morghdari-emqx", "--format", "{{.Name}} {{.State.Health.Status}}"],
                   capture_output=True, text=True).stdout.strip()
R.ok("5.18b", n == "2" and "granted qos=[1]" in logs2, f"rules={n} clean-resubscribe={'granted qos=[1]' in logs2} health=[{h}]")
R.f.flush()
print("WAVE3D DONE", flush=True)
