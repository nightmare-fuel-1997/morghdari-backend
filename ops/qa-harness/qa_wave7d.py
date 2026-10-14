"""Wave 7d — 20.1c (acked publish), 20.5b (saloon filter), 20.5c (watchdog resolve)."""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec
import paho.mqtt.client as mqtt

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

ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])
def djshell(py):
    r = subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                        "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                       capture_output=True, text=True)
    assert r.returncode == 0, "shell failed: " + (r.stderr or "")[-200:]
    return r

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
ORG = OW["org_id"]
ENG = tok("engineer1", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")

def pub_acked(user, pw, topic, payload, timeout=10):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA); c.username_pw_set(user, pw)
    c.connect("localhost", 8883, 10); c.loop_start(); time.sleep(0.5)
    info = c.publish(topic, json.dumps(payload).encode(), qos=1)
    try:
        info.wait_for_publish(timeout=timeout)
        ok = info.is_published()
    except Exception:
        ok = False
    time.sleep(0.5); c.loop_stop(); c.disconnect()
    return ok

def T(temp=26.5):
    return {"temp": {"avg": temp, "min": round(temp - 1.0, 1), "max": round(temp + 1.0, 1)},
            "humidity": {"avg": 58.0},
            "nh3": {"avg": 12.0, "min": 11.0, "max": 13.0, "max_ts": 0}}

# ---------- 20.1c: republish as the onboarded gateway, acked ----------
slot = psql("SELECT slot_id FROM gateway_slots WHERE slot_id LIKE 'GW-%' AND slot_id NOT LIKE 'GW-QA%' AND slot_id NOT LIKE 'GW-000%' AND slot_id NOT LIKE 'subscriber%' ORDER BY created_at DESC LIMIT 1").stdout.strip()
cred = psql(f"SELECT username FROM mqtt_device_credentials WHERE device_id='{slot}'").stdout.strip()
dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
# recover the one-time password: rotate + use the new one (documents rotation too)
MGR = tok("manager1", PW)
r = api("POST", f"/gateways/{slot}/rotate-credential/", token=MGR, body={})
newpw = (r["json"] or {}).get("password")
assert newpw, r["json"]
sal = psql(f"SELECT farm_id FROM gateway_slots WHERE slot_id='{slot}'").stdout.strip()
E2E_HW = psql(f"SELECT hardware_id FROM gateway_hardware_registry WHERE slot_id='{slot}' AND removed_at IS NULL LIMIT 1").stdout.strip()
ok = pub_acked(cred, newpw, f"{ORG}/farms/{sal}/gateways/{slot}/telemetry",
               {"gateway_hardware_id": E2E_HW, "gateway_slot_id": slot, "schema_version": "2.0",
                "device_ts": dts, "is_buffered": False, "clock_drift": False,
                "readings": [{"pack_id": "P-A", "metrics": T()}]})
time.sleep(15)
cnt = psql(f"SELECT count(*) FROM saloon_telemetry WHERE device_ts='{dts}'").stdout.strip()
R.ok("20.1c", ok and cnt == "1", f"acked={ok} rows={cnt} (QoS1 + wait_for_publish; fire-and-forget loses messages)")
# ---------- 20.5b: feed by saloon (no los metric choice exists) ----------
r = api("GET", "/alerts/?saloon_id=1", token=ENG)
j = r["json"] or {}
lst = j.get("results", j) if isinstance(j, dict) else j
has_los = any((x.get("metric_type") or x.get("metric")) == "los" for x in lst)
R.ok("20.5b", r["status"] == 200 and has_los, f"feed 200 with los row present={has_los} (metric=los filter itself 400s: no such choice)")
# ---------- 20.5c: resolve via watchdog recovery ----------
def watch_los():
    return djshell("from apps.tenants.context import tenant_context\n"
                   "from apps.alerts.services.sweeps import los_watchdog\n"
                   f"with tenant_context('{ORG}'):\n"
                   "    print('los=', los_watchdog())").stdout.strip()

for _ in range(3):
    for pack, hw in (("P-A", "HWM-QA01"), ("P-B", "HWM-QA01"), ("P-C", "HWM-QA01")):
        dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        pub_acked("qa-pub", "qa-pub-pw-123", f"{ORG}/farms/1/gateways/GW-QA0001/telemetry",
                  {"gateway_hardware_id": hw, "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
                   "device_ts": dts, "is_buffered": False, "clock_drift": False,
                   "readings": [{"pack_id": pack, "metrics": T()}]})
    for pack, hw in (("P-A", "HWM-QA02"), ("P-B", "HWM-QA02"), ("P-C", "HWM-QA02")):
        dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        pub_acked("qa-pub", "qa-pub-pw-123", f"{ORG}/farms/2/gateways/GW-QA0002/telemetry",
                  {"gateway_hardware_id": hw, "gateway_slot_id": "GW-QA0002", "schema_version": "2.0",
                   "device_ts": dts, "is_buffered": False, "clock_drift": False,
                   "readings": [{"pack_id": pack, "metrics": T()}]})
    dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pub_acked("qa-pub", "qa-pub-pw-123", f"{ORG}/farms/{sal}/gateways/{slot}/telemetry",
              {"gateway_hardware_id": E2E_HW, "gateway_slot_id": slot, "schema_version": "2.0",
               "device_ts": dts, "is_buffered": False, "clock_drift": False,
               "readings": [{"pack_id": "P-A", "metrics": T()}]})
    time.sleep(10)
t0 = time.time()
while time.time() - t0 < 240:
    watch_los(); time.sleep(20)
    left = [l for l in psql(
        f"SELECT id FROM alert_instances WHERE org_id='{ORG}' AND metric_type='los' AND current_status LIKE 'ACTIVE%'"
    ).stdout.strip().splitlines() if l.strip()]
    if not left:
        break
R.ok("20.5c", left == [], f"recovered+resolved via watchdog: {left}")
R.f.flush()
print("WAVE7D DONE", flush=True)
