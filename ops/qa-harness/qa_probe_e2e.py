"""Probe E2E gateway connect."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
PW = "password12345"
MGR = tok("manager1", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")

r = api("POST", "/gateways/GW-84E0A7A50FF6F063/rotate-credential/", token=MGR, body={})
print("rotate:", r["status"], str(r["json"])[:160], flush=True)
newpw = (r["json"] or {}).get("password")
for label, u, p in [("new", "GW-84E0A7A50FF6F063", newpw), ("old", "GW-84E0A7A50FF6F063", "GW-84E0A7A50FF6F063-mqtt-pw")]:
    got = {}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA)
    c.username_pw_set(u, p or "x")
    c.on_connect = lambda cli, ud, fl, rc, pr=None: got.setdefault("rc", rc.value if hasattr(rc, "value") else rc)
    try:
        c.connect("localhost", 8883, 10)
        c.loop_start()
        t0 = time.time()
        while "rc" not in got and time.time() - t0 < 8:
            time.sleep(0.2)
        c.loop_stop(); c.disconnect()
    except Exception as e:
        got["rc"] = type(e).__name__
    print(label, "rc=", got.get("rc"), flush=True)
