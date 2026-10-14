import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok

PW = "password12345"
OWNER = tok("demo-owner", PW)
for p in ("/alerts/", "/farms/"):
    r = api("GET", p, token=OWNER)
    print(p, r["status"])
    j = r["json"]
    if isinstance(j, dict):
        print(" keys:", list(j.keys())[:10])
        items = j.get("results", [])
    else:
        items = j
    print(" n:", len(items) if isinstance(items, list) else type(items))
    if items:
        print(" sample:", json.dumps(items[0], ensure_ascii=False)[:500])
