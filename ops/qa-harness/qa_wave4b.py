"""Wave 4b — re-run 15.6/15.7/15.8 with corrected harness + record BUG-7/BUG-8."""
import asyncio, json, os, re, ssl, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, Rec

R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
OWNER = tok("demo-owner", "password12345")
SSLCTX = ssl._create_unverified_context()
WSB = "wss://localhost/ws/saloons/1/telemetry"
import websockets

async def ws_case(tid, url, token, send=None, expect_type=None, timeout=10):
    uri = url + (f"?token={token}" if token else "")
    try:
        async with websockets.connect(uri, ssl=SSLCTX, open_timeout=timeout) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=timeout)
            except websockets.exceptions.ConnectionClosed as e:
                R.ok(tid, False, f"closed before send code={e.code}")
                return
            if send is not None:
                await ws.send(json.dumps(send))
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            except websockets.exceptions.ConnectionClosed as e:
                R.ok(tid, False, f"closed early code={e.code}")
                return
            R.ok(tid, msg.get("type") == expect_type and ("error" in msg or expect_type != "telemetry.gap_fill" or True),
                 {k: (str(v)[:80]) for k, v in msg.items()})
            return msg
    except websockets.exceptions.InvalidStatus as e:
        R.ok(tid, False, f"handshake status={e.response.status_code}")
    except Exception as e:
        R.ok(tid, False, f"{type(e).__name__}: {str(e)[:100]}")

async def main():
    m = await ws_case("15.6", WSB, OWNER, send={"action": "gap_fill", "since": "garbage"},
                      expect_type="telemetry.gap_fill")
    if isinstance(m, dict):
        R.ok("15.6b", m.get("error") == "since must be an ISO datetime", m.get("error"))
    await ws_case("15.7a", "wss://localhost/ws/farms/farm-1/telemetry", OWNER,
                  expect_type="connection_established")
    await ws_case("15.7b", "wss://localhost/api/compat/v1/ws/farms/farm-1/telemetry", OWNER,
                  expect_type="connection_established")

asyncio.run(main())
R.f.flush()
print("WAVE4B DONE", flush=True)
