"""Restore temp_max=30 + recache + clear grace (post-8.10b)."""
import os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok

HERE = os.path.dirname(os.path.abspath(__file__))
env1 = {}
for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8"):
    m = re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())
    if m:
        env1[m.group(1)] = m.group(2)

MGR = tok("manager1", "password12345")
ADMIN_URL = "postgres://%s:%s@postgres:5432/%s" % (env1["POSTGRES_ADMIN_USER"], env1["POSTGRES_ADMIN_PASSWORD"], env1["DATABASE_NAME"])

rid = subprocess.run(["docker", "exec", "morghdari-postgres", "psql",
    "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
    "-c", "SELECT id FROM lifecycle_threshold_rules WHERE metric='temp' LIMIT 1"],
    capture_output=True, text=True,
    env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()
print("PUT:", api("PUT", f"/thresholds/{rid}/", token=MGR, body={"max": 30})["status"], flush=True)
r = subprocess.run(["docker", "exec", "-i", "-e", "DATABASE_URL=" + ADMIN_URL,
                    "morghdari-daphne", "python", "manage.py", "shell", "-c",
                    "from apps.alerts.services.thresholds import recache_all_saloons\nprint(recache_all_saloons())"],
                   capture_output=True, text=True)
print("recache rc:", r.returncode, flush=True)
g = subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                    "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"], "DEL", "threshold:transition:1"],
                   capture_output=True, text=True).stdout.strip()
print("grace del:", g, flush=True)
v = subprocess.run(["docker", "exec", "morghdari-postgres", "psql",
    "-U", env1["POSTGRES_ADMIN_USER"], "-d", env1["DATABASE_NAME"], "-w", "-t", "-A",
    "-c", "SELECT max_value FROM lifecycle_threshold_rules WHERE metric='temp'"],
                   capture_output=True, text=True,
                   env={**os.environ, "PGPASSWORD": env1["POSTGRES_ADMIN_PASSWORD"]}).stdout.strip()
print("temp_max now:", v, flush=True)
