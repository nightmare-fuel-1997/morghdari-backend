"""15.7a/15.7b re-probe post BUG-7 fix (compat WS routes)."""
import asyncio, json, os, ssl as _ssl, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import tok, Rec
import websockets

R = Rec()
SSLCTX = _ssl._create_unverified_context()
OWNER = tok("demo-owner", "password12345")

async def case(tid, url, expect):
    try:
        async with websockets.connect(url + f"?token={OWNER}", ssl=SSLCTX, open_timeout=10) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            R.ok(tid, msg.get("type") == expect, f"{msg.get('type')} farm={msg.get('farm_slug')}")
    except websockets.exceptions.InvalidStatus as e:
        R.ok(tid, expect == "hs-fail", f"handshake status={e.response.status_code}")
    except Exception as e:
        R.ok(tid, False, f"{type(e).__name__}: {str(e)[:100]}")

async def main():
    await asyncio.sleep(0.4)
    await case("15.7a", "wss://localhost/ws/farms/farm-1/telemetry", "connection_established")
    await asyncio.sleep(0.4)
    await case("15.7b", "wss://localhost/api/compat/v1/ws/farms/farm-1/telemetry", "hs-fail")

asyncio.run(main())
R.f.flush()
print("REPROBE157 DONE", flush=True)
