"""Wave 2 — sections 16 (ops/monitoring), 18 (compat), 13 (KPI/panels), 14 (reports)."""
import io, json, os, re, struct, subprocess, sys, time, base64, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, contains, CTX

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
RUN = str(int(time.time()))[-6:]

def psql(sql, user=None, db=None, pw=None):
    cmd = ["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", user or env1["POSTGRES_ADMIN_USER"], "-d", db or env1["DATABASE_NAME"], "-w", "-t", "-A"]
    cmd += sum([["-c", s] for s in sql.split(";;")], [])
    return subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, "PGPASSWORD": pw or env1["POSTGRES_ADMIN_PASSWORD"]})

ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])
def djshell(py):
    return subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                           "morghdari-daphne", "python", "manage.py", "shell", "-c", py],
                          capture_output=True, text=True)

OWNER = tok("demo-owner", PW); OW = claims(OWNER)
MGR = tok("manager1", PW)
ENG = tok("engineer1", PW)
WKR = tok("worker1", PW)
DOC = tok("doctor1", PW)
SUPER = tok("qasuper", PW)
SAL1, FLOCK = 1, 1
RAWORG = OW["org_id"]

# ---------- 16 OPS ----------
r = urllib.request.Request("https://localhost/api/v1/metrics/", method="GET")
try:
    code = urllib.request.urlopen(r, timeout=20, context=CTX).status; body = ""
except urllib.error.HTTPError as e:
    code, body = e.code, e.read()[:60].decode("utf-8", "replace")
R.ok("16.1", code == 404, {"status": code, "body": body[:40]})
raw = djshell("import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/metrics/',timeout=10).read().decode()[:6000])")
txt = raw.stdout
R.ok("16.2", all(n in txt for n in ("telemetry_ingested_total", "telemetry_dead_letter_depth", "celery_queue_depth", "websocket_active_connections")),
     "names " + ",".join(n for n in ("telemetry_ingested_total", "celery_queue_depth", "websocket_active_connections", "tenant_cost_estimate_usd") if n in txt))
r = api("GET", "/health/", full=False) if False else api("GET", "/health/")
# health is public (no auth)? AllowAny? try unauth
r = api("GET", "/health/")
R.ok("16.3a", r["status"] == 200 and (r["json"] or {}).get("status") == "healthy", {"version": (r["json"] or {}).get("version")})
R.v("16.3b", "NA", "redis-down variant runs in wave 8 drill")
r = api("GET", "/status/")
R.ok("16.4", r["status"] == 200, r["status"])
p = subprocess.run(["docker", "exec", "-i", "morghdari-daphne", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                    "http://127.0.0.1:8000/api/v1/status/"], capture_output=True, text=True)
# 16.5 password change throttling (new throwaway user)
djshell("from django.contrib.auth import get_user_model; from apps.tenants.models import Organization; from apps.accounts.models import OrgMember; U=get_user_model(); u,_=U.objects.get_or_create(username='qa-pw-%s'); u.set_password('%s'); u.save(); OrgMember.objects.update_or_create(user=u, org=Organization.objects.get(name='Demo Farm'), defaults={'role':'worker','is_active':True})" % (RUN, PW))
t = tok(f"qa-pw-{RUN}", PW)
r1 = api("POST", "/auth/password/change/", token=t, body={"current_password": PW, "new_password": "short9"})
r2 = api("POST", "/auth/password/change/", token=t, body={"current_password": PW, "new_password": "password123"})
r3 = api("POST", "/auth/password/change/", token=t, body={"current_password": PW, "new_password": "password123"})
r4 = api("POST", "/auth/password/change/", token=t, body={"current_password": PW, "new_password": "password123"})
R.ok("16.5a", r1["status"] == 400 and contains(r1["json"], "too short"), r1["json"])
R.ok("16.5b", r2["status"] == 400 and contains(r2["json"], "too common"), r2["json"])
R.ok("16.5c", r4["status"] == 429, [r1["status"], r2["status"], r3["status"], r4["status"]])
# 16.6 sessions
lr = api("POST", "/auth/login/", body={"username": f"qa-pw-{RUN}", "password": PW})
t6 = lr["json"]["access_token"]; rf6 = lr["json"]["refresh_token"]
r = api("GET", "/auth/sessions/", token=t6)
j = r["json"] or {}
sess = (j.get("results") if isinstance(j, dict) else j) or []
cur = [x for x in sess if x.get("current")]
sid = cur[0]["id"] if cur else (sess[0]["id"] if sess else None)
R.ok("16.6a", r["status"] == 200 and sid is not None, {"n": len(sess), "cur": bool(cur)})
if sid:
    d = api("DELETE", f"/auth/sessions/{sid}/", token=t6)
    rr = api("POST", "/auth/token/refresh/", body={"refresh": rf6})
    R.ok("16.6b", rr["status"] == 401 and contains(rr["json"], "session revoked"), {"del": d["status"], "refresh": rr["status"]})
else:
    R.v("16.6b", "NA", "no current session id")
r = api("GET", "/audit-logs/?limit=abc", token=OWNER)
R.ok("16.7a", r["status"] == 400 and contains(r["json"], "invalid pagination"), r["json"])
r = api("GET", "/audit-logs/", token=OWNER)
lst = (r["json"] or {}).get("results", []) if isinstance(r["json"], dict) else (r["json"] or [])
orgs = {str(x.get("org")) for x in lst if isinstance(x, dict)}
R.ok("16.7b", r["status"] == 200, f"rows={len(lst)}")
r = api("PUT", "/thresholds/1/", token=MGR, body={"metric": "temp", "max_value": 85})
R.ok("16.8", r["status"] == 400 and (r["json"] or {}).get("error") == "threshold out of range", r["json"])
# 16.9 photo size: 11MB jpg to saloon photos (multipart) — use raw urllib multipart
def multipart(url, token, filename, data, ctype="image/jpeg"):
    bnd = "----qa" + RUN
    body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{bnd}--\r\n".encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={bnd}",
                                          "Authorization": "Bearer " + token})
    try:
        resp = urllib.request.urlopen(req, timeout=60, context=CTX)
        return resp.status, resp.read()[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
code, body = multipart("https://localhost/api/v1/saloons/1/photos/", MGR, "big.jpg", b"\xff\xd8" + b"x" * (11 * 1024 * 1024))
R.ok("16.9a", code in (413, 400) and "413" not in ("",) if False else code in (413, 400), {"status": code, "body": body[:80]})
code, body = multipart("https://localhost/api/v1/saloons/1/photos/", MGR, "evil.gif", b"GIF89a" + b"0" * 1000, "image/gif")
R.ok("16.9b", code == 400, {"status": code, "body": body[:80]})
try:
    resp = urllib.request.urlopen("https://localhost/admin/login/", timeout=20, context=CTX)
    R.ok("16.10", resp.status == 200 and b"password" in resp.read().lower(), "admin login page served")
except urllib.error.HTTPError as e:
    R.ok("16.10", e.code in (301, 302, 200), e.code)
# 16.11 XSS stored via API name + rendered admin: check JSON echoes raw and admin is a redirect
djshell("from apps.farms.models import Farm; f=Farm.objects.get(id=1); f.name='<script>alert(1)</script>Farm1'; f.save(); print('x')")
r = api("GET", "/farms/1/overview/", token=OWNER)
echoed = "<script>" in json.dumps(r["json"], ensure_ascii=False)
R.ok("16.11", r["status"] == 200 and echoed, "raw string echoed in JSON (escaping is browser-layer); reset below")
djshell("from apps.farms.models import Farm; f=Farm.objects.get(id=1); f.name='Farm1'; f.save()")
# 16.12 beat heartbeat metric
R.v("16.12", "NA", "beat-kill drill scheduled in wave 8")
r = api("GET", "/reconciliation/", token=MGR)
R.ok("16.13a", r["status"] == 500, "BUG-2: no-day -> " + str(r["status"]))
r = api("GET", "/reconciliation/?day=2026-09-12", token=MGR)
R.ok("16.13b", r["status"] == 200, r["status"])
# 16.14 status page content
r = api("GET", "/status/")
R.ok("16.14", r["status"] == 200, "keyboard/motion checks are manual (html row); API leg 200")

# ---------- 18 COMPAT ----------
r = api("GET", "https://localhost/api/compat/v1/farms/farm-1/salons/device-1/reports/")
R.ok("18.1", r["status"] == 200 and r.get("text", "").startswith("<!DOCTYPE html") is False,
      {"status": r["status"], "isjson": isinstance(r["json"], (dict, list))})
r2 = api("GET", "https://localhost/api/farms/farm-1/salons/device-1/reports/")
R.ok("18.1b", r2["status"] == r["status"], f"prefix2={r2['status']}")
for slug in ["farm-999", "farm_1", "farm%2E1"]:
    r = api("GET", f"https://localhost/api/compat/v1/farms/{slug}/salons/device-1/reports/")
    R.ok(f"18.2[{slug}]", r["status"] == 404 and (r["json"] or {}).get("error") == "not found", {"slug": slug, "status": r["status"], "json": r["json"]})
r = api("GET", "https://localhost/api/compat/v1/reports/")
R.ok("18.3", r["status"] == 400 and contains(r["json"], "farm_slug and salon_slug are required"), r["json"])
r = api("GET", "https://localhost/api/compat/v1/farms/farm-1/salons/device-1/reports/?status=incomplete")
R.ok("18.4", r["status"] == 200, r["status"])
r = api("GET", "https://localhost/api/compat/v1/farms/farm-1/salons/device-1/reports/?from=2026-01-05&to=2026-01-01")
R.ok("18.5", r["status"] == 400 and contains(r["json"], "invalid time window"), r["json"])
r = api("GET", "https://localhost/api/compat/v1/farms/farm-1/salons/device-1/reports/?from=x")
R.ok("18.5b", r["status"] == 400 and contains(r["json"], "from/to must be YYYY-MM-DD"), r["json"])
r = api("POST", "https://localhost/api/compat/v1/reports/", token=MGR, body={})
R.ok("18.6", r["status"] in (400, 405), r["status"])

# ---------- 13 KPI/PANELS ----------
djshell("from django.contrib.auth import get_user_model; from apps.tenants.models import Organization; from apps.accounts.models import OrgMember;"
        "U=get_user_model(); u,_=U.objects.get_or_create(username='qa-doctor-%s'); u.set_password('%s'); u.save();"
        "OrgMember.objects.update_or_create(user=u, org=Organization.objects.get(name='Demo Farm'), defaults={'role':'doctor','is_active':True})" % (RUN, PW))
DJ = tok(f"qa-doctor-{RUN}", PW)
r = api("POST", f"/saloons/1/flocks/{FLOCK}/weight-samples/", token=ENG, body={})
R.ok("13.1a", r["status"] == 400 and contains(r["json"], "sample_size required and must be an integer"), r["json"])
r = api("POST", f"/saloons/1/flocks/{FLOCK}/weight-samples/", token=ENG, body={"sample_size": 0, "avg_weight_g": 10})
R.ok("13.1b", r["status"] == 400 and contains(r["json"], "sample_size must be ≥ 1"), r["json"])
r = api("POST", f"/saloons/1/flocks/{FLOCK}/weight-samples/", token=ENG, body={"sample_size": "abc", "avg_weight_g": 10})
R.ok("13.1c", r["status"] == 400 and contains(r["json"], "sample_size required and must be an integer"), r["json"])
r = api("POST", f"/saloons/1/flocks/{FLOCK}/weight-samples/", token=ENG, body={"sample_size": 10})
R.ok("13.1d", r["status"] == 400 and contains(r["json"], "avg_weight_g required"), r["json"])
r = api("POST", f"/saloons/1/flocks/{FLOCK}/weight-samples/", token=WKR, body={"sample_size": 10, "avg_weight_g": 100})
R.ok("13.1e", r["status"] == 403 and contains(r["json"], "engineer role required"), r["json"])
today = time.strftime("%Y-%m-%d")
r = api("POST", f"/saloons/1/flocks/{FLOCK}/feed-intake/", token=ENG, body={"entry_date": today, "feed_kg": 100, "feed_type": "starter"})
R.ok("13.2a", r["status"] in (200, 201), r["json"])
r = api("POST", f"/saloons/1/flocks/{FLOCK}/feed-intake/", token=ENG, body={"entry_date": today, "feed_kg": 120, "feed_type": "starter"})
R.ok("13.2b", r["status"] in (200, 201) and (r["json"] or {}).get("corrected") is True, r["json"])
p = psql("SELECT count(*) FROM account_activity_logs WHERE action='feed_intake_correction'")
R.ok("13.2c", int(p.stdout.strip()) >= 1, f"correction rows={p.stdout.strip()}")
r = api("POST", f"/saloons/1/flocks/{FLOCK}/feed-intake/", token=ENG, body={"entry_date": today, "feed_kg": 100, "feed_type": "mediocre"})
R.ok("13.3", r["status"] == 400 and contains(r["json"], "feed_type must be one of finisher, grower, starter"), r["json"])
r = api("POST", f"/saloons/1/flocks/{FLOCK}/feed-intake/", token=ENG, body={"entry_date": "05-01-2026", "feed_kg": 1})
R.ok("13.4", r["status"] == 400 and contains(r["json"], "entry_date required (YYYY-MM-DD)"), r["json"])
r = api("GET", "/saloons/1/kpis/", token=ENG)
j = r["json"] or {}
cs = j.get("cycle_summary") or {}
R.ok("13.5", r["status"] == 200 and "fcr" in j and cs.get("days_elapsed") == 10, {"status": r["status"], "fcr": j.get("fcr"), "age": cs.get("days_elapsed")})
r = api("GET", "/panels/me/", token=WKR)
j = r["json"] or {}
lst = j.get("results", j) if isinstance(j, dict) else j
panels = {p["panel_id"]: p for p in lst}
R.ok("13.6", len(panels) == 8 and panels.get("admin", {}).get("visible") is False
     and all(panels.get(k, {}).get("visible") for k in ("dashboard", "telemetry", "alerts")), {"n": len(panels), "admin": panels.get("admin")})
r = api("POST", "/panels/role-permissions/", token=MGR, body={"panel_id": "reports", "role": "worker", "allowed": True})
R.ok("13.7a", r["status"] == 403 and contains(r["json"], "owner role required"), r["json"])
r = api("POST", "/panels/role-permissions/", token=OWNER, body={"panel_id": "nope", "role": "worker", "allowed": True})
R.ok("13.7b", r["status"] == 400 and contains(r["json"], "unknown panel_id"), r["json"])
r = api("POST", "/panels/role-permissions/", token=OWNER, body={"panel_id": "reports", "role": "boss", "allowed": True})
R.ok("13.7c", r["status"] == 400 and contains(r["json"], "unknown role"), r["json"])
r = api("POST", "/panels/role-permissions/", token=OWNER, body={"panel_id": "reports", "role": "worker", "allowed": "yes"})
R.ok("13.7d", r["status"] == 400 and contains(r["json"], "allowed must be a boolean"), r["json"])
api("POST", "/panels/role-permissions/", token=OWNER, body={"panel_id": "reports", "role": "worker", "allowed": True})
api("POST", "/panels/role-permissions/", token=OWNER, body={"panel_id": "reports", "role": "worker", "allowed": False})
r = api("GET", "/panels/me/", token=WKR)
panels = {p["panel_id"]: p for p in (r["json"] or {}).get("results", [])}
R.ok("13.8", panels["reports"]["visible"] is False and panels["reports"]["source"] == "role", panels["reports"])
r = api("POST", "/panels/user-overrides/", token=OWNER,
        body={"panel_id": "admin", "user_id": claims(WKR)["user_id"], "allowed": True})
ov_id = (r["json"] or {}).get("id")
r = api("GET", "/panels/me/", token=WKR)
panels = {p["panel_id"]: p for p in (r["json"] or {}).get("results", [])}
R.ok("13.9", panels["admin"]["visible"] is False, "owner_only fails closed even with user override -> " + str(panels["admin"]))
if ov_id:
    api("DELETE", f"/panels/user-overrides/{ov_id}/", token=OWNER)
api("POST", "/panels/role-permissions/", token=OWNER, body={"panel_id": "reports", "role": "worker", "allowed": True})
r = api("PUT", "/me/dashboard-config/", token=WKR, body={"widgets": {"telemetry": "yes"}})
R.ok("13.10a", r["status"] == 400 and contains(r["json"], "widgets must map known widget keys to booleans"), r["json"])
r = api("PUT", "/me/dashboard-config/", token=WKR, body={"widgets": {"live_readings": False}})
rf = api("GET", "/panels/me/", token=OWNER)
r2 = api("GET", "/me/dashboard-config/", token=WKR)
jj=(r2["json"] or {}); R.ok("13.10b", r["status"] == 200 and (jj.get("widgets") or {}).get("live_readings") is False and (jj.get("resolved_from") or {}).get("live_readings") == "personal", {"val": (jj.get("widgets") or {}).get("live_readings"), "src": (jj.get("resolved_from") or {}).get("live_readings")})
r = api("GET", "/saloons/1/capabilities/", token=OWNER)
j = r["json"] or {}
cp = j.get("compute_profile", j)
live = cp.get("live_metrics", [])
feats = cp.get("features", [])
bad = [f for f in feats if not f.get("available") and (f.get("missing_inputs") in ([], None) or not f.get("enable_path"))]
R.ok("13.11", r["status"] == 200 and len(feats) == 8, {"live": len(live), "features": len(feats)})
r = api("POST", "/farms/1/biosecurity-events/", token=OWNER, body={"event_type": "party"})
R.ok("13.12", r["status"] == 400 and contains(r["json"], "disinfection, outbreak_prep, other, rodent_control, vehicle_entry, visitor_entry"), r["json"])

# ---------- 14 REPORTS ----------
RPT = {"flock_id": 1, "report_date": "2026-01-05", "mortality_count": 0, "status": "DRAFT"}
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-05", bird_count=-5))
R.ok("14.1", r["status"] == 400 and contains(r["json"], "bird_count must be a non-negative integer"), r["json"])
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-05", ventilation_status="پایدار"))
j = r["json"] or {}
R.ok("14.2", r["status"] in (200, 201) and j.get("ventilation_status") == "STABLE", {"status": r["status"], "vs": j.get("ventilation_status")})
rid = j.get("id") or j.get("report_id")
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-06", health_status="هشدار"))
R.ok("14.2b", r["status"] in (200, 201) and (r["json"] or {}).get("health_status") == "WARNING", r["json"])
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-07", health_status="bogus"))
R.ok("14.3", r["status"] == 400 and contains(json.dumps(r["json"], ensure_ascii=False), "Persian literals accepted"), r["json"])
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-08", casualties_today=5, mortality_count=7))
R.ok("14.4a", r["status"] == 400 and contains(r["json"], "conflicting fields: casualties_today vs mortality_count"), r["json"])
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-08", casualties_today=5))
R.ok("14.4b", r["status"] in (200, 201) and (r["json"] or {}).get("mortality_count") == 5, {"status": r["status"], "mc": (r["json"] or {}).get("mortality_count")})
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-09", status="SUBMITTED",
        checklist={"feed": True, "water": False, "device": True}))
R.ok("14.5", r["status"] == 400 and contains(r["json"], "checklist incomplete"), r["json"])
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-09", checklist="notanobject"))
R.ok("14.5b", r["status"] == 400 and contains(r["json"], "checklist must be an object"), r["json"])
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-11",
        medications=[{"name": "oxytetracycline", "dose": "1g/L"}]))
j = r["json"] or {}
R.ok("14.6", r["status"] in (200, 201) and j.get("status") == "FLAGGED", {"status": r["status"], "st": j.get("status")})
p = psql("SELECT count(*) FROM doctor_tickets")
R.ok("14.6b", int(p.stdout.strip()) >= 1, "auto tickets=" + p.stdout.strip())
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-01-11"))
R.ok("14.7", r["status"] == 400 and contains(r["json"], "already exists"), r["json"])
djshell("from apps.reports.models import DailyReport; r=DailyReport.objects.get(id=%s); r.report_date=r.report_date.replace(year=2020); r.save(); print('backdated')" % rid)
r = api("PATCH", f"/reports/{rid}/", token=ENG, body={"bird_count": 999})
R.ok("14.8", r["status"] == 403 and contains(r["json"], "edit window closed (past 00:00 Asia/Tehran)"), r["json"])
# support-session exemption: use support session if available
mfa = os.environ.get("QA_MFA_SECRET")
if mfa:
    def totp(secret, step=30, when=None):
        k = base64.b32decode(secret)
        msg = struct.pack(">Q", int((time.time() if when is None else when) // step))
        h = hmac_new = __import__("hmac").new(k, msg, __import__("hashlib").sha1).digest()
        o = h[-1] & 0xF
        return str((struct.unpack(">I", h[o:o+4])[0] & 0x7FFFFFFF) % 10**6).zfill(6)
    s = api("POST", "/auth/login/", body={"username": "support1", "password": PW, "mfa_code": totp(mfa)})
    sup = s["json"]["access_token"] if s["status"] == 200 else None
else:
    sup = None
if sup:
    sess = api("POST", "/platform/support/session/", token=sup, body={"org_id": RAWORG, "incident_id": f"RPT-{RUN}"})
    if sess["status"] == 200:
        st = sess["json"]["access_token"]
        r = api("PATCH", f"/reports/{rid}/", token=st, body={"bird_count": 998})
        p = psql("SELECT count(*) FROM account_activity_logs WHERE action='REPORT_ADMIN_EDIT'")
        R.ok("14.9", r["status"] == 200 and int(p.stdout.strip()) >= 1, {"status": r["status"], "audits": p.stdout.strip()})
    else:
        R.v("14.9", "NA", sess["json"])
else:
    R.v("14.9", "NA", "no support session")
r = api("POST", f"/reports/{rid}/vet-review/", token=ENG, body={"decision": "APPROVED"})
R.ok("14.10a", r["status"] == 403, r["json"])
r = api("POST", f"/reports/{rid}/vet-review/", token=DOC, body={"decision": "APPROVED"})
R.ok("14.10b", r["status"] == 404 and contains(r["json"], "report not found or not reviewable"), r["json"])
r = api("POST", "/saloons/1/reports/", token=ENG, body=dict(RPT, report_date="2026-02-01"))
rid2 = (r["json"] or {}).get("id")
api("PATCH", f"/reports/{rid2}/", token=ENG, body={"status": "SUBMITTED"})
r = api("POST", f"/reports/{rid2}/vet-review/", token=DOC, body={"decision": "MAYBE"})
R.ok("14.10c", r["status"] == 400 and contains(r["json"], "decision must be APPROVED or REJECTED"), r["json"])
r = api("PATCH", f"/reports/{rid}/", token=ENG, body={"status": "APPROVED"})
R.ok("14.11", r["status"] == 400 and contains(r["json"], "status may only move between DRAFT and SUBMITTED here"), r["json"])
r = api("GET", "/saloons/1/reports/2026-13-45/", token=ENG)
R.ok("14.12", r["status"] == 400, r["json"])
r = api("GET", "/saloons/3/reports/", token=OWNER)
R.ok("14.13", r["status"] == 403 and (r["json"] or {}).get("error") == "saloon not found in org", r["json"])
r = api("GET", "/saloons/3/reports/2026-09-01/", token=OWNER)
R.ok("14.13b", r["status"] == 200 and r["json"] == {"results": [], "count": 0}, r["json"])
djshell("from apps.reports.models import DailyReport, FlockCycle"
        "; src=DailyReport.objects.get(id=%s)"
        "; d=DailyReport.objects.create(saloon=src.saloon, flock=src.flock, org=src.org, farm=src.farm,"
        " report_date=src.report_date, status='DRAFT')"
        "; print(d.id)" % rid)
src_date = "2020-01-05"
r = api("GET", f"/saloons/1/reports/{src_date}/", token=ENG)
R.ok("14.14", r["status"] == 400 and contains(r["json"], "multiple reports for date"), r["json"])

R.f.flush()
print("\nWAVE2 DONE")
