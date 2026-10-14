"""Board cleanup: force-resolve all ACTIVE alerts + clear stale blobs/mutes."""
import base64, hashlib, hmac, os, re, struct, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api

HERE = os.path.dirname(os.path.abspath(__file__))
env1 = {}
for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8"):
    m = re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())
    if m:
        env1[m.group(1)] = m.group(2)

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

sec = os.environ.get("QA_MFA_SECRET", "NFQKOVY4YKEXEUI7WFSVRSLBTBSUEDWY")
k = base64.b32decode(sec)
msg = struct.pack(">Q", int(time.time() // 30))
h = hmac.new(k, msg, hashlib.sha1).digest()
o = h[-1] & 0xF
code = str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)
st = api("POST", "/auth/login/", body={"username": "support1", "password": "password12345", "mfa_code": code})["json"]["access_token"]
org = psql("SELECT org_id FROM organizations WHERE name='Demo Farm'")
supt = api("POST", "/platform/support/session/", token=st,
           body={"org_id": org, "incident_id": "QA-CLEAN"})["json"]["access_token"]
ids = [l for l in psql("SELECT id FROM alert_instances WHERE current_status LIKE 'ACTIVE%'").splitlines() if l.strip()]
print(f"resolving {len(ids)}", flush=True)
for aid in ids:
    r = api("POST", "/platform/support/alerts/" + aid + "/force-resolve/", token=supt, body={"reason": "QA board cleanup"})
    print(aid[:8], r["status"], flush=True)
for key in redis("KEYS", "alert:active:*").splitlines():
    key = key.strip()
    if key:
        redis("DEL", key)
for key in redis("KEYS", "saloon:*:breach:*").splitlines():
    key = key.strip()
    if key:
        redis("DEL", key)
print("blobs cleared", flush=True)
print("qa mutes/subs:", psql("DELETE FROM maintenance_mutes WHERE reason LIKE 'qa%'"), psql("DELETE FROM alert_subscriptions"), flush=True)
print("CLEAN DONE", flush=True)
