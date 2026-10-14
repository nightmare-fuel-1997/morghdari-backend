"""Wave 10c — 16.12: celery-beat killed -> heartbeat gauge freezes, staleness > 300s; restart recovers."""
import os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import Rec

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))

def metrics_beat_ts():
    out = subprocess.run(
        ["docker", "exec", "morghdari-daphne", "python", "-c",
         "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/metrics/', timeout=10).read().decode())"],
        capture_output=True, text=True)
    m = re.search(r"^celery_beat_heartbeat_timestamp_seconds\s+([\d.eE+-]+)", out.stdout, re.M)
    return float(m.group(1)) if m else None

g0 = metrics_beat_ts()
assert g0, "beat gauge unreadable pre-drill"
subprocess.run(["docker", "compose", "-p", "morghdari", "-f", "docker-compose.prod.yml",
                "-f", "docker-compose.vps.yml", "stop", "celery-beat"], capture_output=True)
print("beat stopped", flush=True)
try:
    time.sleep(70)
    g1 = metrics_beat_ts()
    frozen = (g1 == g0)
    # keep stopped until staleness window is comfortably > 300s
    while time.time() - g0 <= 330:
        time.sleep(15)
    g2 = metrics_beat_ts()
    staleness = round(time.time() - g2, 1)
    frozen2 = (g2 == g0)
    rule_holds = staleness > 300  # alerts.yml BeatHeartbeatStale: time() - max(...) > 300
finally:
    subprocess.run(["docker", "compose", "-p", "morghdari", "-f", "docker-compose.prod.yml",
                    "-f", "docker-compose.vps.yml", "start", "celery-beat"], capture_output=True)
# recovery: gauge advances once beat schedules the heartbeat task again
rec = False
t0 = time.time()
while time.time() - t0 < 240:
    g = metrics_beat_ts()
    if g and g > g0:
        rec = True
        break
    time.sleep(15)

R.ok("16.12", frozen and frozen2 and rule_holds and rec,
     f"gauge frozen at {g0} while beat down (2 reads equal); staleness {staleness}s > 300 (BeatHeartbeatStale rule); advanced after restart={rec}")
R.f.flush()
print("WAVE10C DONE", flush=True)
