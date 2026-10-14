"""Probe E2E connect correctly: commit the staged rotation, then connect."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
PW = "password12345"
MGR = tok("manager1", PW)
CA = os.path.join(HERE, "..", "certs", "ca.crt")
NEWPW = "KFm1z4-BnrsFun0pYUbGuUKEQ29O8_uhnbcS_BDomHk"

r = api("POST", "/gateways/GW-84E0A7A50FF6F063/rotation-ack/", token=MGR, body={"new_password": NEWPW})
print("ack:", r["status"], r["json"], flush=True)

def tc(pw):
    got = {}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.tls_set(ca_certs=CA)
    c.username_pw_set("GW-84E0A7A50FF6F063", pw)
    c.on_connect = lambda cli, ud, fl, rc, pr=None: got.setdefault("rc", rc.value if hasattr(rc, "value") else rc)
    try:
        c.connect("localhost", 8883, 10)
        c.loop_start()
        t0 = time.time()
        while "rc" not in got and time.time() - t0 < 8:
            time.sleep(0.2)
        c.loop_stop(); c.disconnect()
    except Exception:
        got["rc"] = "ERR"
    return got.get("rc")

print("committed pw rc=", tc(NEWPW), flush=True)
