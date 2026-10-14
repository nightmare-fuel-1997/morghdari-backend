"""Wave 3c — corrected re-runs: v5 reason codes + SUBACK with cache-busting wait."""
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

V5DENY = {4, 5, 134, 135}
R.ok("6.3", try_connect("GW-QA0001", "wrong-pw") in V5DENY, "rc=134 (v5 Bad User Name or Password)")
R.ok("6.4", try_connect("GW-QASUSP", "susp-pw-1") in V5DENY, "rc=135 (v5 Not Authorized): SUSPENDED filtered by authn SQL")
R.ok("6.5", try_connect("qa-inact", "inact-pw-1") in V5DENY, "rc=135: is_active=false filtered")
R.ok("6.9d", try_connect("GW-QA0003", "GW-QA0003-mqtt-pw") in V5DENY, "old password rejected post-commit")

# 5.18 with EMQX authz-cache-busting wait (default cache TTL ~60s)
bak = psql("SELECT username||'|'||topic||'|'||permission||'|'||action FROM mqtt_acl_rules WHERE username='subscriber-01'").stdout
subprocess.run(["docker", "stop", "morghdari-subscriber-01"], capture_output=True)
psql("DELETE FROM mqtt_acl_rules WHERE username='subscriber-01'")
print("rules deleted; waiting out the broker authz cache...", flush=True)
time.sleep(80)
subprocess.run(["docker", "start", "morghdari-subscriber-01"], capture_output=True)
time.sleep(20)
logs = subprocess.run(["docker", "logs", "morghdari-subscriber-01", "--since", "120s"], capture_output=True, text=True).stdout
denied = "SUBACK DENIED" in logs and "resubscribing in 5s" in logs
for row in [l for l in bak.strip().splitlines() if l.strip()]:
    u, t, pm, a = [x.strip() for x in row.split("|")]
    psql(f"INSERT INTO mqtt_acl_rules (username,topic,permission,action) VALUES ('{u}','{t}','{pm}','{a}')")
n = psql("SELECT count(*) FROM mqtt_acl_rules WHERE username='subscriber-01'").stdout.strip()
subprocess.run(["docker", "restart", "morghdari-subscriber-01"], capture_output=True)
time.sleep(18)
logs2 = subprocess.run(["docker", "logs", "morghdari-subscriber-01", "--since", "90s"], capture_output=True, text=True).stdout
R.ok("5.18", denied and n == "2" and "granted qos=[1]" in logs2,
     f"denied+retry={denied} restored={n} clean-resubscribe={'granted qos=[1]' in logs2}")
R.f.flush()
print("WAVE3C DONE", flush=True)
