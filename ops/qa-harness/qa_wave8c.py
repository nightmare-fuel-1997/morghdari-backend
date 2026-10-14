"""Wave 8c — 17.4 redis kill -9 (AOF survival) + 17.10 password change restart."""
import os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import Rec

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

def psql(sql):
    return subprocess.run(["docker", "exec", "-i", "morghdari-postgres", "psql",
           "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
           "-c", sql], capture_output=True, text=True,
           env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]})

x0 = int(redis("XLEN", "telemetry:realtime"))
h0 = redis("HGETALL", "saloon:1:thresholds")
subprocess.run(["docker", "kill", "-s", "9", "morghdari-redis"], capture_output=True)
time.sleep(5)
st = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", "morghdari-redis"],
                    capture_output=True, text=True).stdout.strip()
subprocess.run(["docker", "start", "morghdari-redis"], capture_output=True)
t0 = time.time(); back = False
while time.time() - t0 < 120:
    time.sleep(5)
    try:
        if redis("PING") == "PONG":
            back = True
            break
    except Exception:
        pass
x1 = int(redis("XLEN", "telemetry:realtime") or -1)
h1 = redis("HGETALL", "saloon:1:thresholds")
R.ok("17.4", back and x1 >= x0 and h1 == h0 and len(h1) > 0,
     f"back={back} xlens {x0}->{x1} thresholds_intact={bool(h1)}")
R.f.flush()
print("WAVE8C DONE", flush=True)
