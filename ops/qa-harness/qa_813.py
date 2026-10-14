"""QA 8.13 — live probe: muted 'thi' + co-present temp/humidity must not crash the drain (BUG-20 fix)."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
RUN = str(int(time.time()))[-6:]
ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])

def djshell(py):
    r = subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                        "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                       capture_output=True, text=True)
    assert r.returncode == 0, "shell failed: " + (r.stderr or "")[-300:]
    return r

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

# fix present in the RUNNING image?
g = subprocess.run(["docker", "exec", "morghdari-celery-worker", "grep", "-c", "BUG-20",
                    "apps/alerts/services/evaluation.py"], capture_output=True, text=True)
fixed = g.stdout.strip() == "1"

OWNER = tok("demo-owner", "password12345"); ORG = claims(OWNER)["org_id"]
tehran = dt.timezone(dt.timedelta(hours=3, minutes=30))
day = dt.datetime.now(tehran).date().isoformat()

# create the thi mute (org-wide AlertSubscription)
djshell(
    "from apps.alerts.models import AlertSubscription\n"
    "from apps.tenants.models import Organization\n"
    "from apps.accounts.models import User\n"
    f"AlertSubscription.objects.filter(org__org_id='{ORG}', metric_key='thi').delete()\n"
    f"AlertSubscription.objects.create(org=Organization.objects.get(org_id='{ORG}'), metric_key='thi', "
    f"muted_by=User.objects.get(username='demo-owner'), reason='qa 8.13 {RUN}')\n"
    "print('mute-created')"
)
before_alerts = psql("SELECT count(*) FROM alert_instances WHERE org_id='%s' AND metric_type='thi'" % ORG).stdout.strip()

# publish co-present temp+humidity (THI-capable) readings via MQTT TLS
sys.path.insert(0, os.path.join(HERE))
try:
    from qa_wave7c import publish_qa, ORG as _WORG  # reuse helper if importable
except Exception:
    publish_qa = None

import paho.mqtt.client as mqtt
import ssl as _ssl
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub(payload):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA)
    c.username_pw_set("GW-QA0001", "GW-QA0001-mqtt-pw")
    c.connect("localhost", 8883, 10)
    c.loop_start(); time.sleep(0.4)
    c.publish(f"{ORG}/farms/1/gateways/GW-QA0001/telemetry", json.dumps(payload).encode(), qos=1)
    time.sleep(0.4); c.loop_stop(); c.disconnect()

from datetime import datetime, timezone
for i in range(3):
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pub({"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
         "device_ts": dts, "is_buffered": False, "clock_drift": False,
         "readings": [{"pack_id": "P-A", "metrics": {
             "temp": {"avg": 35.0, "min": 34.0, "max": 36.0},
             "humidity": {"avg": 70.0, "min": 69.0, "max": 71.0}}}]})

# wait for drain (poll the digest key, bounded)
t0 = time.time(); digest = None
while time.time() - t0 < 90:
    d = subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                        "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"],
                        "GET", f"submuted:{ORG}:thi:{day}"],
                       capture_output=True, text=True).stdout.strip()
    if d and d != "(nil)" and int(d) >= 1:
        digest = d
        break
    time.sleep(5)

logs = subprocess.run(["docker", "logs", "--since", "5m", "morghdari-celery-worker"],
                      capture_output=True, text=True)
crash = ("UnboundLocalError" in logs.stdout + logs.stderr) or ("NameError" in logs.stdout + logs.stderr)
after_alerts = psql("SELECT count(*) FROM alert_instances WHERE org_id='%s' AND metric_type='thi'" % ORG).stdout.strip()

R.ok("8.13", fixed and digest is not None and not crash and after_alerts == before_alerts,
     f"fix-in-image={fixed} digest={digest} crash={crash} thi_alerts {before_alerts}->{after_alerts}")

# cleanup the mute
djshell(f"from apps.alerts.models import AlertSubscription\n"
        f"AlertSubscription.objects.filter(org__org_id='{ORG}', metric_key='thi').delete()\nprint('mute-gone')")
R.f.flush()
print("QA813 DONE", flush=True)
