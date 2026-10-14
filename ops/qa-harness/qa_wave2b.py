"""Wave 2b — re-run the wave-2 leftovers with corrected fixtures."""
import json, os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, contains

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
RUN = str(int(time.time()))[-6:]

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

OWNER = tok("demo-owner", PW); ENG = tok("engineer1", PW); DOC = tok("doctor1", PW); WKR = tok("worker1", PW)

# ---------- 18 compat, authenticated ----------
CB = "https://localhost/api/compat/v1"
r = api("GET", f"{CB}/farms/farm-1/salons/device-1/reports/", token=OWNER)
R.ok("18.1", r["status"] == 200, {"status": r["status"]})
r = api("GET", "https://localhost/api/farms/farm-1/salons/device-1/reports/", token=OWNER)
R.ok("18.1b", r["status"] == 200, r["status"])
r = api("GET", f"{CB}/farms/farm-999/salons/device-1/reports/", token=OWNER)
R.ok("18.2a", r["status"] == 404 and (r["json"] or {}).get("error") == "not found", r["json"])
r = api("GET", f"{CB}/farms/farm_1/salons/device-1/reports/", token=OWNER)
R.ok("18.2b", r["status"] == 404 and r["json"] is None, "routing-level HTML 404 (underscore fails slug regex)")
r = api("GET", f"{CB}/reports/", token=OWNER)
R.ok("18.3", r["status"] == 400 and contains(r["json"], "farm_slug and salon_slug are required"), r["json"])
r = api("GET", f"{CB}/farms/farm-1/salons/device-1/reports/?status=incomplete", token=OWNER)
R.ok("18.4", r["status"] == 200, r["status"])
r = api("GET", f"{CB}/farms/farm-1/salons/device-1/reports/?from=2026-01-05&to=2026-01-01", token=OWNER)
R.ok("18.5", r["status"] == 400 and contains(r["json"], "invalid time window"), r["json"])
r = api("GET", f"{CB}/farms/farm-1/salons/device-1/reports/?from=x", token=OWNER)
R.ok("18.5b", r["status"] == 400 and contains(r["json"], "from/to must be YYYY-MM-DD"), r["json"])
r = api("POST", f"{CB}/reports/", token=OWNER, body={})
R.ok("18.6", r["status"] in (400, 405), r["status"])

# ---------- 13.12 exact enum order ----------
r = api("POST", "/farms/1/biosecurity-events/", token=OWNER, body={"event_type": "party"})
R.ok("13.12", r["status"] == 400 and r["json"] == {"error": "event_type must be one of disinfection, other, outbreak_prep, rodent_control, vehicle_entry, visitor_entry"}, r["json"])

# ---------- 14.4b alias-only (no canonical key) ----------
r = api("POST", "/saloons/1/reports/", token=ENG,
        body={"flock_id": 1, "report_date": f"2026-04-{RUN[4:]}", "status": "DRAFT", "casualties_today": 5})
R.ok("14.4b", r["status"] in (200, 201) and (r["json"] or {}).get("mortality_count") == 5, {"status": r["status"], "mc": (r["json"] or {}).get("mortality_count")})

# ---------- 14.6 medications proper schema + ticket growth ----------
before = int(psql("SELECT count(*) FROM support_tickets").stdout.strip())
r = api("POST", "/saloons/1/reports/", token=ENG,
        body={"flock_id": 1, "report_date": f"2026-05-{RUN[4:]}", "mortality_count": 0, "status": "DRAFT",
              "medications": [{"medication_name": "oxytetracycline", "dosage": "1g/L", "frequency": "daily"}]})
j = r["json"] or {}
after = int(psql("SELECT count(*) FROM support_tickets").stdout.strip())
R.ok("14.6", r["status"] in (200, 201) and j.get("status") == "FLAGGED", {"status": r["status"], "st": j.get("status")})
R.ok("14.6b", after == before + 1, f"tickets {before}->{after}")

# ---------- 14.10c bad decision on a FLAGGED report ----------
r = api("POST", "/reports/5/vet-review/", token=DOC, body={"decision": "MAYBE"})
R.ok("14.10c", r["status"] == 400 and contains(r["json"], "decision must be APPROVED or REJECTED"), r["json"])

# ---------- 14.11 status-move 400 on an in-window report ----------
r = api("POST", "/saloons/1/reports/", token=ENG,
        body={"flock_id": 1, "report_date": time.strftime("%Y-%m-%d"), "mortality_count": 0, "status": "DRAFT"})
ridw = (r["json"] or {}).get("id")
r = api("PATCH", f"/reports/{ridw}/", token=ENG, body={"status": "APPROVED"})
R.ok("14.11", r["status"] == 400 and contains(r["json"], "status may only move between DRAFT and SUBMITTED here"), r["json"])

# ---------- 14.14 deemed unreachable: DB unique blocks the state ----------
p = psql("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='uq_daily_reports_flock_saloon_date'")
ok = "uq_daily_reports_flock_saloon_date" in p.stdout and "UNIQUE" in p.stdout
R.ok("14.14", ok, p.stdout.strip()[:160])

R.f.flush()
print("\nWAVE2B DONE")
