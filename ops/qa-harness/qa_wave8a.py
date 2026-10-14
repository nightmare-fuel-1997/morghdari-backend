"""Wave 8a — infra quick checks (no restarts except daphne-kill 17.9)."""
import json, os, re, subprocess, sys, time
import urllib.request
import urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, Rec, CTX

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

# ---------- 17.5 concurrent health burst ----------
import threading
codes = []
def hit():
    try:
        r = urllib.request.urlopen("http://127.0.0.1:80/api/v1/health/", timeout=15)
        codes.append(r.status)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
    except Exception as e:
        codes.append(type(e).__name__)
ts = [threading.Thread(target=hit) for _ in range(40)]
[t.start() for t in ts]; [t.join() for t in ts]
from collections import Counter
cc = Counter(codes)
R.ok("17.5", set(codes) <= {200, 429} and 200 in codes, dict(cc))

# ---------- 17.6 redirect + healthz ----------
def get(url, follow=False):
    class NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NR)
    try:
        r = opener.open(url, timeout=15)
        return r.status, r.headers.get("Location", ""), r.read()[:60]
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", ""), b""
r = get("http://127.0.0.1/anything")
R.ok("17.6a", r[0] == 301 and r[1].startswith("https://"), (r[0], r[1]))
r = get("http://127.0.0.1/healthz")
R.ok("17.6b", r[0] == 200 and b"ok" in r[2], (r[0], r[2]))

# ---------- 17.7 TLS versions + headers ----------
OPENSSL = "C:\\Program Files\\Git\\usr\\bin\\openssl.exe"
tls12 = subprocess.run([OPENSSL, "s_client", "-connect", "127.0.0.1:443", "-tls1_2"],
                       input=b"", capture_output=True, timeout=30)
R.ok("17.7a", b"handshake failure" in tls12.stdout.lower() or tls12.returncode != 0 or b"no protocols available" in tls12.stderr.lower(),
     "TLS1.2 refused")
req = urllib.request.Request("https://localhost/api/v1/health/")
h = urllib.request.urlopen(req, timeout=20, context=CTX).headers
need = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}
okh = all(h.get(k) == v for k, v in need.items()) and "max-age=63072000" in (h.get("Strict-Transport-Security") or "")
srv = h.get("Server", "")
R.ok("17.7b", okh and "nginx" not in srv.lower() or (okh and "/" not in srv), f"headers ok={okh} server={srv!r}")

# ---------- 17.8 no token in logs ----------
logs = subprocess.run(["docker", "logs", "morghdari-nginx", "--since", "600m"],
                      capture_output=True, text=True).stdout
R.ok("17.8", "token=" not in logs, f"log lines with token=: {logs.count('token=')}")

# ---------- 17.9 health x9 + 17.11 fail-fast ----------
hh = subprocess.run(["docker", "inspect", "-f", "{{.Name}} {{.State.Health.Status}}",
                     "morghdari-nginx", "morghdari-emqx", "morghdari-redis", "morghdari-postgres",
                     "morghdari-daphne", "morghdari-subscriber-01", "morghdari-subscriber-02",
                     "morghdari-celery-worker", "morghdari-celery-beat"],
                    capture_output=True, text=True).stdout
R.ok("17.9", "healthy" in hh and "unhealthy" not in hh and "starting" not in hh, hh.replace("\n", " ")[:200])
ff = subprocess.run(["docker", "compose", "-p", "morghdari", "-f", "docker-compose.prod.yml",
                     "-f", "docker-compose.vps.yml", "run", "--rm", "--no-deps",
                     "-e", "DJANGO_ALLOWED_HOSTS=", "-e", "DATABASE_URL=postgres://x@y/z",
                     "daphne", "python", "-c", "import config.settings.production"],
                    capture_output=True, text=True, cwd=os.path.join(HERE, ".."))
R.ok("17.11", ff.returncode != 0 and ("ALLOWED_HOSTS" in (ff.stdout + ff.stderr) or "RuntimeError" in (ff.stdout + ff.stderr)),
     (ff.stdout + ff.stderr)[-200:])

# ---------- 19.8 bootstrap rerun ----------
b = subprocess.run(["bash", "scripts/prod_bootstrap.sh"], capture_output=True, text=True,
                   cwd=os.path.join(HERE, ".."), timeout=300)
R.ok("19.8", b.returncode == 0 and "bootstrap complete" in (b.stdout + b.stderr), (b.stdout + b.stderr)[-200:])

# ---------- 19.10 / 19.11 ----------
h = subprocess.run(["python", "scripts/check_hil_readiness.py", "--skip-live"],
                   capture_output=True, text=True, cwd=os.path.join(HERE, ".."))
R.ok("19.10", h.returncode == 0, (h.stdout + h.stderr)[-160:])
o = subprocess.run(["python", "scripts/ota_checksums.py", "scripts/qa_wave7a.py", "--version", "9.9.9",
                    "--url", "https://example.invalid/fw.bin", "--out", "C:/Users/mrboz/AppData/Local/Temp/opencode/qa_ota.json"],
                   capture_output=True, text=True, cwd=os.path.join(HERE, ".."))
import glob as _g
sidecars = _g.glob(os.path.join(HERE, "..", "scripts", "qa_wave7a.py*")) + \
           _g.glob("C:/Users/mrboz/AppData/Local/Temp/opencode/qa_ota.json")
R.ok("19.11a", o.returncode == 0, (o.stdout + o.stderr)[-160:])
m = subprocess.run(["python", "scripts/ota_checksums.py", "scripts/NOPE.bin", "--version", "1",
                    "--url", "https://example.invalid/x", "--out", "x.json"],
                   capture_output=True, text=True, cwd=os.path.join(HERE, ".."))
R.ok("19.11b", m.returncode == 2, f"missing artifact rc={m.returncode}")

# ---------- 19.9 kill -9 daphne ----------
import urllib.request as _u
subprocess.run(["docker", "exec", "morghdari-daphne", "sh", "-c", "kill -9 1"], capture_output=True)
codes = []
t0 = time.time()
while time.time() - t0 < 90:
    try:
        r = _u.urlopen("http://127.0.0.1:80/api/v1/health/", timeout=5)
        codes.append(r.status)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
    except Exception as e:
        codes.append(type(e).__name__)
    h = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", "morghdari-daphne"],
                       capture_output=True, text=True).stdout.strip()
    if h == "healthy" and 200 in codes:
        break
    time.sleep(5)
R.ok("19.9", "healthy" in subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}",
    "morghdari-daphne"], capture_output=True, text=True).stdout and 200 in codes and "Timeout" not in str(codes),
    f"codes={codes[:12]}")
R.f.flush()
print("WAVE8A DONE", flush=True)
