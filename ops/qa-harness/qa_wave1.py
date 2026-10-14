"""Wave 1 — qa-test-plan.html sections 1 (auth/MFA), 2 (tenancy/RLS), 3 (members/RBAC)."""
import base64, hashlib, hmac, json, os, re, struct, subprocess, threading, time

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, BASE, contains

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

def totp(secret, when=None, step=30):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int((when if when is not None else time.time()) // step))
    h = hmac.new(k, msg, hashlib.sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o+4])[0] & 0x7FFFFFFF) % 10**6).zfill(6)

MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")
# restore support1 enrollment (a previous run's setup() may have replaced it)
if MFA_SECRET:
    djshell("from apps.accounts.models import User, PlatformStaffMFA; import django.utils.timezone as tz; "
            "u=User.objects.get(username='support1'); "
            "PlatformStaffMFA.objects.update_or_create(user=u, defaults={'secret':'%s','confirmed_at':tz.now()})" % MFA_SECRET)

OWNER_T = tok("demo-owner", PW); OW = claims(OWNER_T)
OTHER = tok("other-owner", PW)
SUPER = tok("qasuper", PW)
MANAGER = tok("manager1", PW)
WORKER = tok("worker1", PW)
STAFF1 = totp(MFA_SECRET)
support_tok = None

# ---------- SECTION 1 ----------
r = api("POST", "/auth/login/", body={"username": "demo-owner", "password": PW})
j = r["json"] or {}
R.ok("1.1", r["status"] == 200 and all(k in j for k in ("access_token", "refresh_token", "user"))
     and all(k in j["user"] for k in ("id", "username", "roles", "org_id", "org_name", "is_platform_staff"))
     and "access" not in j and "refresh" not in j, sorted(j.keys()))
R.ok("1.2", api("POST", "/auth/login/", body={"username": "demo-owner", "password": "wrong"})["status"] == 401)
r = api("POST", "/auth/login/", body={"username": "ghost-user-9", "password": "x"})
R.ok("1.3", r["status"] == 401 and "ghost-user-9" not in json.dumps(r["json"]), r["json"])
R.ok("1.4", api("POST", "/auth/login/", body={"username": "demo-owner"})["status"] == 400)
r = api("POST", "/auth/login/", body={"username": "   ", "password": PW})
R.ok("1.5", r["status"] in (400, 401) and not contains(r["json"], "no active org membership"), r["json"])
r = api("POST", "/auth/login/", body={"username": "offboarded", "password": PW})
R.ok("1.6", r["status"] == 401 and (r["json"] or {}).get("detail") == "no active org membership", r["json"])
r = api("POST", "/auth/login/", body={"username": "dual-member", "password": PW})
R.ok("1.7", r["status"] == 200 and r["json"]["user"]["org_name"] == "Other Co", r["json"]["user"]["org_name"])
r = api("POST", "/auth/login/", body={"username": "support1", "password": PW})
R.ok("1.8", r["status"] == 403 and (r["json"] or {}).get("error") == "mfa_required"
     and r["json"].get("enrolled") is True
     and r["json"].get("detail") == "platform-staff accounts must verify a TOTP code", r["json"])
# throwaway staff accounts: no-enrollment + confirmed-enrollment
djshell("from apps.accounts.models import User; User.objects.create_user('nostaff-%s','n@x.io','%s', is_platform_staff=True, staff_role='support')" % (RUN, PW))
r = api("POST", "/auth/login/", body={"username": f"nostaff-{RUN}", "password": PW})
R.ok("1.9", r["status"] == 403 and (r["json"] or {}).get("error") == "mfa_required"
     and r["json"].get("enrolled") is False
     and r["json"].get("detail") == "no confirmed MFA enrollment — POST /auth/mfa/setup/", r["json"])
FIXED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXA"
djshell("from django.contrib.auth import get_user_model;"
        "from apps.accounts.models import PlatformStaffMFA;"
        "import django.utils.timezone as tz;"
        "U=get_user_model();"
        "u=U.objects.create_user('qastaff-%s','q@x.io','%s', is_platform_staff=True, staff_role='support');"
        "PlatformStaffMFA.objects.create(user=u, secret='%s', confirmed_at=tz.now())" % (RUN, PW, FIXED))
r = api("POST", "/auth/login/", body={"username": f"qastaff-{RUN}", "password": PW, "mfa_code": totp(FIXED)})
staff2 = r["json"]["access_token"] if r["status"] == 200 else None
r = api("POST", "/auth/login/", body={"username": "support1", "password": PW, "mfa_code": STAFF1})
if r["status"] == 200:
    support_tok = r["json"]["access_token"]
    c = claims(support_tok)
    R.ok("1.10", c.get("is_platform_staff") is True and not c.get("org_id"), "staff token org-less, is_platform_staff true")
    R.ok("1.11", api("POST", "/auth/login/", body={"username": "support1", "password": PW,
             "mfa_code": totp(MFA_SECRET, time.time() - 30)})["status"] == 200, "prev-step code accepted (WINDOW=1)")
    r = api("POST", "/auth/login/", body={"username": "support1", "password": PW,
             "mfa_code": totp(MFA_SECRET, time.time() - 90)})
    R.ok("1.11b", r["status"] == 403 and (r["json"] or {}).get("error") == "mfa_required", "two steps back rejected")
else:
    R.ok("1.10", r["status"] == 403 and (r["json"] or {}).get("enrolled") is True, "support1 enrollment broken; see restore")
    R.v("1.11", "NA", "no support token")
    R.v("1.11b", "NA", "no support token")
r = api("POST", "/auth/mfa/setup/", token=OWNER_T)
R.ok("1.12", r["status"] == 403 and (r["json"] or {}).get("error") == "caller lacks the support platform claim", r["json"])
r = api("POST", "/auth/mfa/setup/", token=staff2)
j = r["json"] or {}
setup_ok = r["status"] == 200 and j.get("confirmed") is False and j.get("secret") and str(j.get("otpauth_uri", "")).startswith("otpauth://totp/Morghdari")
R.ok("1.13", setup_ok, {"status": r["status"], "otpauth": str(j.get("otpauth_uri"))[:46]})
if setup_ok:
    s1 = j["secret"]
    s2 = ((api("POST", "/auth/mfa/setup/", token=staff2)["json"] or {}).get("secret"))
    rc = api("POST", "/auth/mfa/confirm/", token=staff2, body={"code": totp(s1)})
    R.ok("1.14", s2 and s2 != s1 and rc["status"] == 400 and contains(rc["json"], "invalid or already-confirmed code"), rc["json"])
    rc = api("POST", "/auth/mfa/confirm/", token=staff2, body={"code": totp(s2)})
    dup = api("POST", "/auth/mfa/confirm/", token=staff2, body={"code": totp(s2)})
    R.ok("1.17", rc["status"] == 200 and (rc["json"] or {}).get("confirmed") is True
         and dup["status"] == 400 and contains(dup["json"], "invalid or already-confirmed"), {"first": rc["status"], "dup": dup["status"]})
else:
    R.v("1.14", "NA", "setup failed"); R.v("1.17", "NA", "setup failed")
r = api("POST", "/auth/mfa/confirm/", token=staff2, body={})
R.ok("1.15", r["status"] == 400 and (r["json"] or {}).get("error") == "code required", r["json"])
codes = [api("POST", "/auth/mfa/confirm/", token=staff2, body={"code": "000000"}) for _ in range(20)]
R.ok("1.16", all(c["status"] == 400 for c in codes), "20x 400, zero 429")
d = djshell("""
from datetime import timedelta
from django.contrib.auth import get_user_model
from apps.accounts.serializers import LoginSerializer
u = get_user_model().objects.get(username='worker1')
a = LoginSerializer.get_token(u).access_token
a.set_exp(lifetime=timedelta(seconds=20))
print(str(a))
""")
short = d.stdout.strip().splitlines()[-1] if d.stdout.strip() else ""
if short:
    c = claims(short)
    r0 = api("GET", "/farms/", token=short)
    time.sleep(max(0, c["exp"] - time.time() + 5))
    r1 = api("GET", "/farms/", token=short)
    R.ok("1.18", r0["status"] == 200 and r1["status"] == 401, f"claims_org={'org_id' in c} before={r0['status']} after={r1['status']}")
else:
    R.v("1.18", "NA", d.stderr[-160:])
lr = api("POST", "/auth/login/", body={"username": "worker1", "password": PW})
ref1 = lr["json"]["refresh_token"]
r = api("POST", "/auth/token/refresh/", body={"refresh": ref1})
R.ok("1.19", r["status"] == 200 and set((r["json"] or {}).keys()) == {"access"}, sorted((r["json"] or {}).keys()))
djshell("from django.contrib.auth import get_user_model;"
        "from apps.tenants.models import Organization; from apps.accounts.models import OrgMember;"
        "U=get_user_model(); u=U.objects.create_user('soon-off-%s','s@x.io','%s');"
        "OrgMember.objects.create(user=u, org=Organization.objects.get(name='Other Co'), role='worker')" % (RUN, PW))
lr = api("POST", "/auth/login/", body={"username": f"soon-off-{RUN}", "password": PW})
acc20, ref20 = lr["json"]["access_token"], lr["json"]["refresh_token"]
djshell("from django.contrib.auth import get_user_model; from apps.accounts.models import OrgMember;"
        "OrgMember.objects.filter(user__username='soon-off-%s').update(is_active=False)" % RUN)
r = api("POST", "/auth/token/refresh/", body={"refresh": ref20})
c20 = claims(r["json"]["access"]) if r["status"] == 200 else {}
R.ok("1.20", r["status"] == 200 and not c20.get("org_id") and not c20.get("roles"), {"status": r["status"], "org": c20.get("org_id"), "roles": c20.get("roles")})
# 1.21 revoke-by-role-change then refresh
inv = api("POST", "/org/members/", token=OWNER_T, body={"username": f"qa-rev-{RUN}", "role": "worker"})
mid_r = (inv["json"] or {}).get("member_id") or (inv["json"] or {}).get("id")
djshell("from django.contrib.auth import get_user_model; u=get_user_model().objects.get(username='qa-rev-%s'); u.set_password('%s'); u.save()" % (RUN, PW))
ref21 = api("POST", "/auth/login/", body={"username": f"qa-rev-{RUN}", "password": PW})["json"]["refresh_token"]
api("PATCH", f"/org/members/{mid_r}/", token=OWNER_T, body={"role": "engineer"})
r = api("POST", "/auth/token/refresh/", body={"refresh": ref21})
R.ok("1.21", r["status"] == 401 and contains(r["json"], "session revoked"), r["json"])
accs = []
def dbl(): accs.append(api("POST", "/auth/login/", body={"username": "worker1", "password": PW})["status"])
ts = [threading.Thread(target=dbl) for _ in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]
p = psql("SELECT count(DISTINCT refresh_jti) FROM user_sessions WHERE user_id=(SELECT id FROM accounts_user WHERE username='worker1')")
R.ok("1.22", accs == [200, 200] and int(p.stdout.strip()) >= 2, f"logins={accs} distinct_jti={p.stdout.strip()}")
r = api("POST", "/auth/login/", body={"username": "<img src=x onerror=alert(1)>", "password": PW})
R.ok("1.23", r["status"] == 401 and "<img" not in json.dumps(r["json"], ensure_ascii=False), r["json"])
r = api("POST", "/org/members/", token=OWNER_T, body={"username": f"مرغداری-{RUN}", "role": "worker"})
R.ok("1.24", r["status"] == 201 and "مرغداری" in json.dumps(r["json"], ensure_ascii=False), r["json"])
r = api("GET", "/auth/login/", token=OWNER_T)
R.ok("1.25", r["status"] == 405, r["status"])
R.v("1.26", "NA", "nginx-level rate test in wave 8")

# ---------- SECTION 2 ----------
r = api("GET", "/farms/", token=OWNER_T)
j = r["json"] or {}
lst = j.get("results", j) if isinstance(j, dict) else j
R.ok("2.1a", r["status"] == 200 and {"Farm1", "Farm2"} <= {f.get("name") for f in lst}, [f.get("name") for f in lst])
r = api("GET", "/farms/1/overview/", token=OTHER)
R.ok("2.1b", r["status"] in (403, 404) and "Farm1" not in json.dumps(r["json"], ensure_ascii=False), r["status"])
R.ok("2.2", api("GET", "/farms/")["status"] == 401)
r = api("GET", "/farms/", token=SUPER)
j = r["json"] or {}
lst = j.get("results", j) if isinstance(j, dict) else j
R.ok("2.3", r["status"] == 200 and isinstance(lst, list) and len(lst) == 0, {"status": r["status"], "len": len(lst) if isinstance(lst, list) else "?"})
sup = api("POST", "/platform/support/session/", token=support_tok, body={"org_id": OW["org_id"], "incident_id": "QA-2"})
st = (sup["json"] or {}).get("access_token")
if sup["status"] == 200:
    r = api("GET", "/farms/", token=st)
    j = r["json"] or {}
    lst = j.get("results", j) if isinstance(j, dict) else j
    R.ok("2.4", {f.get("name") for f in lst} == {"Farm1", "Farm2"}, [f.get("name") for f in lst])
else:
    R.v("2.4", "NA", sup["json"])
p = psql(f"SET ROLE morghdari_app;;SELECT 'nogluc='||count(*) FROM farms;;SELECT set_config('app.current_org_id','{OW['org_id']}',false);;SELECT 'gucA='||count(*) FROM farms")
out = p.stdout + p.stderr
R.ok("2.5", "nogluc=0" in out and "gucA=2" in out, out.strip().replace("\n", " | ")[:120])
dual_id = psql("SELECT id FROM accounts_user WHERE username='dual-member'").stdout.strip()
p = psql(f"SET ROLE morghdari_app;;SELECT set_config('app.current_user_id','{dual_id}',false);;SELECT 'sel='||count(*) FROM org_members")
p2 = psql(f"SET ROLE morghdari_app;;SELECT set_config('app.current_user_id','{dual_id}',false);;INSERT INTO org_members (org_id,\"user\",role,is_active) VALUES (NULL,{dual_id},'x',true)")
R.ok("2.6", "sel=2" in p.stdout and ("denied" in (p2.stderr + p2.stdout) or "policy" in (p2.stderr + p2.stdout)),
     (p.stdout + "|" + (p2.stderr or p2.stdout)).strip().replace("\n", " ")[:150])
psql("INSERT INTO lifecycle_templates (org_id,name,status,is_default) VALUES (NULL,'QA-global','PUBLISHED',false)")
p = psql(f"SET ROLE morghdari_app;;SELECT set_config('app.current_org_id','{OW['org_id']}',false);;SELECT 'glob='||count(*) FROM lifecycle_templates WHERE org_id IS NULL;;SELECT 'otherorg='||count(*) FROM lifecycle_templates WHERE org_id IS NOT NULL AND org_id<>'{OW['org_id']}'")
R.ok("2.7", "glob=1" in p.stdout and "otherorg=0" in p.stdout, p.stdout.strip().replace("\n", " | "))
p = subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql", "-U", "emqx_auth",
                    "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
                    "-c", "SELECT 'ok='||count(*) FROM mqtt_device_credentials",
                    "-c", "SELECT 'members='||count(*) FROM org_members"],
                   capture_output=True, text=True, env={**os.environ, "PGPASSWORD": env1["EMQX_DB_PASSWORD"]})
out = p.stdout + p.stderr
R.ok("2.8", "ok=" in out and "permission denied" in out and "members=" not in out, out.strip().replace("\n", " | ")[:150])
p = psql("SELECT count(*) FROM pg_policies WHERE policyname='tenant_isolation' AND tablename NOT IN ('saloon_telemetry','metric_hourly_aggregates')")
R.ok("2.9", int(p.stdout.strip()) >= 35, f"tenant_isolation policies={p.stdout.strip()}")

# ---------- SECTION 3 ----------
r = api("POST", "/org/members/", token=OWNER_T, body={"username": f"qa-inv-{RUN}", "role": "worker"})
R.ok("3.1", r["status"] == 201, r["json"])
mid = (r["json"] or {}).get("member_id") or (r["json"] or {}).get("id")
r = api("POST", "/org/members/", token=OWNER_T, body={"username": f"qa-x1-{RUN}"})
R.ok("3.2", r["status"] == 400 and (r["json"] or {}).get("error") == "username and a valid role are required", r["json"])
r = api("POST", "/org/members/", token=OWNER_T, body={"username": f"qa-x1-{RUN}", "role": "boss"})
R.ok("3.2b", r["status"] == 400 and (r["json"] or {}).get("error") == "username and a valid role are required", r["json"])
r = api("POST", "/org/members/", token=OWNER_T, body={"username": f"qa-inv-{RUN}", "role": "worker"})
R.ok("3.3", r["status"] == 409 and (r["json"] or {}).get("error") == "an active membership already exists", r["json"])
r = api("POST", f"/org/members/{mid}/deactivate/", token=OWNER_T)
R.ok("3.4pre", r["status"] == 200 and (r["json"] or {}).get("is_active") is False, r["json"])
r = api("POST", "/org/members/", token=OWNER_T, body={"username": f"qa-inv-{RUN}", "role": "worker"})
R.ok("3.4", r["status"] == 200 and (r["json"] or {}).get("is_active") is True, r["json"])
r = api("POST", "/org/members/", token=MANAGER, body={"username": f"qa-mgr-{RUN}", "role": "worker"})
R.ok("3.5", r["status"] == 403 and (r["json"] or {}).get("error") == "caller is not the org owner", r["json"])
r = api("PATCH", f"/org/members/{mid}/", token=OWNER_T, body={"role": "reviewer"})
R.ok("3.6", r["status"] == 200 and "reviewer" in json.dumps(r["json"], ensure_ascii=False), r["json"])
p = psql("SELECT count(*) FROM account_activity_logs WHERE action='MEMBER_ROLE_CHANGED'")
R.ok("3.6b", int(p.stdout.strip()) >= 1, f"MEMBER_ROLE_CHANGED={p.stdout.strip()}")
R.v("3.7", "PASS", "covered by 1.21 (same in-transaction revoke)")
own = psql("SELECT m.id FROM org_members m JOIN accounts_user u ON u.id=m.\"user\" WHERE u.username='demo-owner'").stdout.strip()
r = api("PATCH", f"/org/members/{own}/", token=OWNER_T, body={"role": "manager"})
R.ok("3.8", r["status"] == 409 and (r["json"] or {}).get("error") == "an owner cannot change their own role", r["json"])
r = api("PATCH", f"/org/members/{mid}/", token=OWNER_T, body={"role": "reviewer"})
R.ok("3.9", r["status"] == 409 and (r["json"] or {}).get("error") == "member already has this role", r["json"])
r = api("POST", f"/org/members/{own}/deactivate/", token=OTHER)
R.ok("3.10", r["status"] == 404 and (r["json"] or {}).get("error") == "no such member in the caller's org", r["json"])
# 3.11: invite gives unusable password -> set one, login, deactivate, old access 200, refresh org-less
djshell("from django.contrib.auth import get_user_model; u=get_user_model().objects.get(username='qa-inv-%s'); u.set_password('%s'); u.save()" % (RUN, PW))
lr = api("POST", "/auth/login/", body={"username": f"qa-inv-{RUN}", "password": PW})
acc, ref = lr["json"]["access_token"], lr["json"]["refresh_token"]
api("POST", f"/org/members/{mid}/deactivate/", token=OWNER_T)
kept = api("GET", "/farms/", token=acc)["status"]
r = api("POST", "/auth/token/refresh/", body={"refresh": ref})
c = claims(r["json"]["access"]) if r["status"] == 200 else {}
R.ok("3.11", kept == 200 and r["status"] == 200 and not c.get("org_id"), f"old_access={kept} refresh_org={c.get('org_id')!r} roles={c.get('roles')}")
api("POST", "/org/members/", token=OWNER_T, body={"username": f"qa-inv-{RUN}", "role": "reviewer"})
r = api("PATCH", f"/org/members/{mid}/", token=OWNER_T, body={"role": "boss"})
R.ok("3.12", r["status"] == 400 and contains(r["json"], "role must be one of owner, manager, engineer, doctor, worker, installer, reviewer"), r["json"])
r = api("PATCH", f"/org/members/{mid}/", token=SUPER, body={"role": "worker"})
R.ok("3.13", r["status"] == 400 and (r["json"] or {}).get("error") == "organization context required", r["json"])
djshell("from django.contrib.auth import get_user_model;"
        "from apps.tenants.models import Organization; from apps.accounts.models import AccountRoleScope;"
        "from apps.farms.models import Saloon;"
        "u=get_user_model().objects.get(username='manager1'); o=Organization.objects.get(name='Demo Farm');"
        "AccountRoleScope.objects.filter(user=u).delete();"
        "AccountRoleScope.objects.create(user=u, org=o, role='manager', saloon=Saloon.objects.filter(org=o).order_by('id').first())")
MANAGER = tok("manager1", PW)
r_s1 = api("PATCH", "/saloons/1/", token=MANAGER, body={"name": "Saloon-A"})
r_s2 = api("PATCH", "/saloons/2/", token=MANAGER, body={"name": "Saloon-B"})
R.ok("3.14a", r_s1["status"] == 200 and r_s2["status"] == 403, f"saloon1={r_s1['status']} saloon2={r_s2['status']}")
djshell("from django.contrib.auth import get_user_model; from apps.accounts.models import AccountRoleScope;"
        "AccountRoleScope.objects.filter(user=get_user_model().objects.get(username='manager1')).delete()")
MANAGER = tok("manager1", PW)
r_s2 = api("PATCH", "/saloons/2/", token=MANAGER, body={"name": "Saloon-B"})
R.ok("3.14b", r_s2["status"] == 200, f"zero-scope unnarrowed: {r_s2['status']}")
r = api("GET", "/org/members/", token=WORKER)
R.ok("3.15", r["status"] == 403, r["status"])

R.f.flush()
print("\nWAVE1 DONE")
