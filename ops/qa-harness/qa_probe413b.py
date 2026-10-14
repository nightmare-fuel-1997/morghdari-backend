"""Probe 4.13b: location-tree cache lifecycle with status checks."""
import os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims

HERE = os.path.dirname(os.path.abspath(__file__))
env1 = {}
for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8"):
    m = re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())
    if m:
        env1[m.group(1)] = m.group(2)

PW = "password12345"
OWNER = tok("demo-owner", PW)
ORG = claims(OWNER)["org_id"]

def redis(*args):
    return subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                           "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"]] + list(args),
                          capture_output=True, text=True).stdout.strip()

r = api("GET", "/provisioning/location-tree/", token=OWNER)
print("GET1:", r["status"], flush=True)
print("db0:", redis("KEYS", "*location*"), flush=True)
print("db1:", redis("-n", "1", "KEYS", "*location*"), flush=True)
