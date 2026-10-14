"""15.9 re-probe after daphne restart (fresh channels-redis pool)."""
import os, re, socket, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec
import ssl as _ssl

R = Rec()
PW = "password12345"
OWNER = tok("demo-owner", PW)
r = api("GET", "/saloons/", token=OWNER)
sals = r["json"]["results"] if isinstance(r["json"], dict) else r["json"]
SALA = int([s for s in sals if s["name"] == "Saloon-A"][0].get("saloon_id") or [s for s in sals if s["name"] == "Saloon-A"][0]["id"])

s = _ssl._create_unverified_context().wrap_socket(
    socket.create_connection(("127.0.0.1", 443), timeout=20), server_hostname="localhost")
req = (f"GET /ws/saloons/{SALA}/telemetry?token={OWNER} HTTP/1.1\r\nHost: localhost\r\n"
       "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
       "Sec-WebSocket-Version: 13\r\n\r\n")
s.sendall(req.encode())
s.settimeout(20)
first = s.recv(4096).decode("latin1", "replace")
assert first.startswith("HTTP/1.1 101"), first[:80]
t_up = time.time()
closed_at = None
s.settimeout(340)
while time.time() - t_up < 340:
    try:
        data = s.recv(4096)
    except socket.timeout:
        break
    except (ConnectionResetError, OSError):
        closed_at = time.time()
        break
    if not data:
        closed_at = time.time()
        break
try:
    s.close()
except Exception:
    pass
elapsed = round(closed_at - t_up, 1) if closed_at else None
R.ok("15.9", elapsed is not None and 290 <= elapsed <= 325,
     f"idle-close at {elapsed}s (nginx proxy_read_timeout 300s)")
R.f.flush()
print("REPROBE159 DONE", flush=True)
