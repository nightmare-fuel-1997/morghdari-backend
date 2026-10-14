"""Probe 12.7: config endpoint under RLS for each caller type."""
import base64
import os
import re
import sys
import urllib.request
import urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, CTX

PW = "password12345"
OWNER = tok("demo-owner", PW)
MGR = tok("manager1", PW)

def get(path, auth=None):
    hdrs = {}
    if auth:
        hdrs["Authorization"] = auth
    req = urllib.request.Request("https://localhost/api/v1" + path, headers=hdrs)
    try:
        resp = urllib.request.urlopen(req, timeout=20, context=CTX)
        return resp.status, resp.read()[:160]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:160]

def basic(u, p):
    return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()

for label, auth in [
    ("mgr-jwt", "Bearer " + MGR),
    ("owner-jwt", "Bearer " + OWNER),
    ("no-auth", None),
    ("bad-basic", basic("GW-QA0001", "wrong")),
    ("gw-basic", basic("GW-QA0001", "GW-QA0001-mqtt-pw")),
    ("susp-basic", basic("GW-QASUSP", "susp-pw-1")),
]:
    code, body = get("/gateways/GW-QA0001/config/", auth)
    try:
        body = body.decode()
    except Exception:
        body = repr(body)
    print(f"{label}: {code} {body[:130]}", flush=True)
code, body = get("/gateways/GW-QASUSP/config/", basic("GW-QASUSP", "susp-pw-1"))
print("susp-slot:", code, body[:130] if isinstance(body, bytes) else body, flush=True)
