"""Wave 6b2 — 11.9b/c/d redo with end_date + 11.9d body capture."""
import json, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec

PW = "password12345"
R = Rec()
OWNER = tok("demo-owner", PW)
ENG = tok("engineer1", PW)

r = api("PATCH", "/saloons/4/flocks/4/close/", token=ENG, body={"end_date": "2026-09-13"})
R.ok("11.9b", r["status"] == 200, {"status": r["status"], "json": r["json"]})
r = api("DELETE", "/saloons/4/", token=OWNER)
R.ok("11.9c", r["status"] == 409 and "flock history" in (r["json"] or {}).get("error", ""), r["json"])
r = api("DELETE", "/saloons/5/", token=OWNER)
R.ok("11.9d", r["status"] in (200, 204), {"status": r["status"], "json": r["json"]})
if r["status"] not in (200, 204):
    import subprocess as _sp, os as _os, re as _re
    env1 = dict(m.groups() for line in open(".env", encoding="utf-8")
                if (m := _re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
    print("saloon5 refs:", _sp.run(["docker", "exec", "morghdari-postgres", "psql", "-U",
        env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A", "-c",
        "SELECT 'pos:'||count(*) FROM saloon_positions WHERE saloon_id=5; SELECT 'tel:'||count(*) FROM saloon_telemetry WHERE saloon_id=5; SELECT 'flock:'||count(*) FROM flock_cycles WHERE saloon_id=5;"],
        capture_output=True, text=True,
        env={**_os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip(), flush=True)
R.f.flush()
print("WAVE6B2 DONE", flush=True)
