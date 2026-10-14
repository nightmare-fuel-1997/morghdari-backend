"""Wave 8a2 — redo 17.5 (https, no pacing), 19.8 (git-bash), 19.11a (debug), 19.9 (docker kill)."""
import os, re, subprocess, sys, threading, time
import urllib.request
import urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import Rec, CTX

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
GITBASH = "C:\\Program Files\\Git\\bin\\bash.exe"

codes = []
def hit():
    req = urllib.request.Request("https://localhost/api/v1/health/")
    try:
        r = urllib.request.urlopen(req, timeout=20, context=CTX)
        codes.append(r.status)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
    except Exception as e:
        codes.append(type(e).__name__)
ts = [threading.Thread(target=hit) for _ in range(40)]
t0 = time.time()
[t.start() for t in ts]; [t.join() for t in ts]
from collections import Counter
cc = Counter(codes)
R.ok("17.5", set(codes) <= {200, 429} and 200 in codes and 429 in codes,
     f"{dict(cc)} in {time.time()-t0:.1f}s (burst must trip 429 at least once)")

b = subprocess.run([GITBASH, "scripts/prod_bootstrap.sh"], capture_output=True, text=True,
                   cwd=os.path.join(HERE, ".."), timeout=300)
R.ok("19.8", b.returncode == 0 and "bootstrap complete" in (b.stdout + b.stderr),
     (b.stdout + b.stderr)[-200:])

o = subprocess.run(["python", "scripts/ota_checksums.py", "--help"],
                   capture_output=True, text=True, cwd=os.path.join(HERE, ".."))
print("ota help:", (o.stdout + o.stderr)[:400], flush=True)

subprocess.run(["docker", "kill", "morghdari-daphne"], capture_output=True)
codes = []
t0 = time.time()
recovered = False
while time.time() - t0 < 120:
    try:
        r = urllib.request.urlopen("https://localhost/api/v1/health/", timeout=6, context=CTX)
        codes.append(r.status)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
    except Exception as e:
        codes.append(type(e).__name__)
    h = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", "morghdari-daphne"],
                       capture_output=True, text=True).stdout.strip()
    if h == "healthy" and 200 in codes:
        recovered = True
        break
    time.sleep(5)
R.ok("19.9", recovered and any(c in (502, 503, 504) or c == "URLError" for c in codes),
     f"gap codes={codes[:10]} recovered={recovered}")
R.f.flush()
print("WAVE8A2 DONE", flush=True)
