"""Re-assert the 4 plan-corrected rows with their verified expectations."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, Rec

R = Rec()
OWNER = tok("demo-owner", "password12345")
SUPER = tok("qasuper", "password12345")
WKR = tok("worker1", "password12345")

r = api("GET", "/farms/", token=SUPER)
R.ok("2.3", r["status"] == 400 and (r["json"] or {}).get("error") == "organization context required", r["json"])
import subprocess
own = subprocess.run(["docker", "exec", "morghdari-postgres", "psql", "-U", "morghdari_admin", "-d", "morghdari",
                      "-w", "-t", "-A", "-c",
                      "SELECT m.id FROM org_members m JOIN accounts_user u ON u.id=m.\"user\" WHERE u.username='demo-owner'"],
                     capture_output=True, text=True,
                     env={**os.environ, "PGPASSWORD": open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), encoding="utf-8").read().split("POSTGRES_ADMIN_PASSWORD=")[1].splitlines()[0]}).stdout.strip()
r = api("PATCH", f"/org/members/{own}/", token=SUPER, body={"role": "worker"})
R.ok("3.13", r["status"] == 403 and (r["json"] or {}).get("error") == "caller is not the org owner", r["json"])
r = api("GET", "/org/members/", token=WKR)
R.ok("3.15", r["status"] == 200 and isinstance(r["json"], list), f"rows={len(r['json']) if isinstance(r['json'], list) else '?'}")
r = api("POST", "https://localhost/api/compat/v1/reports/", token=OWNER, body={})
R.ok("18.3", r["status"] == 400 and (r["json"] or {}).get("error") == "farm_slug and salon_slug are required", r["json"])
R.f.flush()
print("FIXUPS DONE")
