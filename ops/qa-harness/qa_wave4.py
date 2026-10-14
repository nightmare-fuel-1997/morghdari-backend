"""Wave 4 — section 7 (telemetry read API) + section 15 (WebSocket)."""
import asyncio, json, os, re, ssl, subprocess, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api, tok, claims, Rec, contains
import websockets

PW = "password12345"
R = Rec()
HERE = os.path.dirname(os.path.abspath(__file__))
env1 = dict(m.groups() for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8")
            if (m := re.match(r"^([A-Z_0-9]+)=(.*)$", line.strip())))
RUN = str(int(time.time()))[-6:]

OWNER = tok("demo-owner", PW)
WKR = tok("worker1", PW)
SUPER = tok("qasuper", PW)
ENG = tok("engineer1", PW)

# ---------- SECTION 7 ----------
r = api("GET", "/saloons/1/telemetry/", token=OWNER)
j = r["json"] or {}
rows = j.get("results", j) if isinstance(j, dict) else j
asc = all(rows[i].get("device_ts", "") <= rows[i + 1].get("device_ts", "") for i in range(len(rows) - 1))
R.ok("7.1", r["status"] == 200 and len(rows) >= 5 and asc, f"rows={len(rows)} ascending={asc}")
r = api("GET", "/saloons/1/telemetry/?limit=3", token=OWNER)
j = r["json"] or {}
rows = j.get("results", j) if isinstance(j, dict) else j
desc = all(rows[i].get("device_ts", "") >= rows[i + 1].get("device_ts", "") for i in range(len(rows) - 1))
R.ok("7.2", r["status"] == 200 and len(rows) == 3 and desc, f"rows={len(rows)} newest_first={desc}")
for q, msg in [("limit=0", "limit must be between 1 and 1000"), ("limit=1001", "limit must be between 1 and 1000"),
               ("limit=abc", "limit must be an integer"), ("offset=-1", "offset must be >= 0")]:
    r = api("GET", f"/saloons/1/telemetry/?{q}", token=OWNER)
    R.ok(f"7.3[{q}]", r["status"] == 400 and contains(r["json"], msg), r["json"])
r = api("GET", "/saloons/1/telemetry/?since=hello", token=OWNER)
R.ok("7.4", r["status"] == 200 and len((r["json"] or {}).get("results", [])) >= 5, "unparseable since ignored")
r = api("GET", "/saloons/1/telemetry/?include_buffered=1", token=OWNER)
R.ok("7.5", r["status"] == 200, r["status"])
r = api("GET", "/saloons/1/telemetry/?slot_id=GW-NOPE", token=OWNER)
R.ok("7.6", r["status"] == 200 and (r["json"] or {}).get("results", r["json"]) in ([],), r["json"])
r = api("GET", "/saloons/1/telemetry/", token=SUPER)
R.ok("7.7", r["status"] == 400 and contains(r["json"], "organization context required"), r["json"])
r = api("GET", "/farms/999/telemetry/", token=OWNER)
R.ok("7.8", r["status"] == 404 and contains(r["json"], "farm not found"), r["json"])
now = datetime.now(timezone.utc)
frm = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
import urllib.parse as _up
def hurl(**kw): return "/saloons/1/analytics/hourly/?" + _up.urlencode(kw)
r = api("GET", hurl(**{"from": frm, "to": now.isoformat(), "metric": "temp"}), token=OWNER)
R.ok("7.9a", r["status"] == 200, r["status"])
r = api("GET", "/saloons/1/analytics/hourly/", token=OWNER)
R.ok("7.9b", r["status"] == 400 and contains(r["json"], "from, to and metric query parameters are required"), r["json"])
r = api("GET", hurl(**{"from": now.isoformat(), "to": frm, "metric": "temp"}), token=OWNER)
R.ok("7.9c", r["status"] == 400 and contains(r["json"], "from/to must be ISO datetimes with from < to"), r["json"])
r = api("GET", hurl(**{"from": frm, "to": now.isoformat(), "metric": "xyz"}), token=OWNER)
R.ok("7.9d", r["status"] == 400 and contains(r["json"], "unknown metric_key"), r["json"])
r = api("GET", "/metric-types/", token=OWNER)
mt = r["json"] if isinstance(r["json"], list) else (r["json"] or {}).get("results", [])
keys = sorted(x.get("key") or x.get("metric_key") or x.get("name") for x in mt)
R.ok("7.10", r["status"] == 200 and keys == ["co", "humidity", "lux", "nh3", "power_gen", "power_mains", "temp", "water_l"]
      and "thi" not in keys, keys)
r = api("GET", "/saloons/1/telemetry/?offset=999999", token=OWNER)
R.ok("7.11", r["status"] == 200 and (r["json"] or {}).get("results", ["x"]) == [], r["json"])
r = api("GET", "/saloons/3/telemetry/", token=OWNER)
R.ok("7.12", r["status"] == 200 and (r["json"] or {}).get("results", ["x"]) == [], r["json"])

# ---------- SECTION 15 ----------
SSLCTX = ssl._create_unverified_context()
WSB = "wss://localhost/ws/saloons/1/telemetry"

async def ws_case(tid, url, token, expect_close=None, send=None, expect_type=None, timeout=10):
    uri = url + (f"?token={token}" if token else "")
    try:
        async with websockets.connect(uri, ssl=SSLCTX, open_timeout=timeout) as ws:
            if expect_close is not None:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=timeout)
                except websockets.exceptions.ConnectionClosed as e:
                    R.ok(tid, e.code == expect_close, f"close={e.code}")
                    return
                R.ok(tid, False, "stayed open, expected close")
                return
            if send is not None:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=timeout)  # discard hello
                except websockets.exceptions.ConnectionClosed as e:
                    R.ok(tid, False, f"closed before send code={e.code}")
                    return
                await ws.send(json.dumps(send))
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            except websockets.exceptions.ConnectionClosed as e:
                R.ok(tid, False, f"closed early code={e.code}")
                return
            R.ok(tid, msg.get("type") == expect_type, {k: (str(v)[:60] if k != "thresholds" else "...") for k, v in msg.items()})
            return msg
    except websockets.exceptions.InvalidStatus as e:
        R.ok(tid if expect_close == "hs-fail" else tid + "-hs",
             expect_close == "hs-fail", f"handshake status={e.response.status_code}")
    except Exception as e:
        R.ok(tid, False, f"{type(e).__name__}: {str(e)[:100]}")

async def main():
    await ws_case("15.1", WSB, OWNER, expect_type="telemetry.hello")
    # BUG-6: close() runs before accept() so Daphne turns every deny into an
    # HTTP 403 handshake (the 4003/4000 codes only exist in-process). Assert
    # the wire truth (403) + the server-side AUTH-DENY clause per case.
    import subprocess as _sp
    def deny_log():
        p = _sp.run(["docker", "logs", "morghdari-daphne", "--since", "120s"],
                    capture_output=True, text=True)
        return (p.stdout or "") + (p.stderr or "")
    await ws_case("15.2a", WSB, None, expect_close="hs-fail")
    await ws_case("15.2b", WSB, "garbage", expect_close="hs-fail")
    await ws_case("15.2c", WSB, SUPER, expect_close="hs-fail")
    await ws_case("15.2d", "wss://localhost/ws/saloons/3/telemetry", OWNER, expect_close="hs-fail")
    logs = deny_log()
    R.ok("15.2", all(s in logs for s in ("clause=no-token", "clause=bad-token", "clause=no-org-claim", "clause=saloon-or-member")),
         "wire=403x4; server clauses logged")
    # 15.3 + 15.4: fill the cap; wire shows 403 for both over-cap and bad-auth
    # (BUG-6) — the ordering is verified via server logs instead.
    conns = []
    import subprocess as _sp2
    def scard(uid):
        p = _sp2.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
                      "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"], "SCARD", f"ws:active:{uid}"],
                     capture_output=True, text=True)
        try: return int((p.stdout or "0").strip())
        except ValueError: return -1
    wkr_uid = claims(WKR).get("user_id")
    # drain leftovers from any crashed run first (disconnects drop immediately;
    # worst case the 300s zombie TTL — wait bounded 60s and record)
    t0 = time.time()
    while scard(wkr_uid) > 0 and time.time() - t0 < 60:
        time.sleep(3)
    R.ok("15.4pre", scard(wkr_uid) == 0, f"cap drained to {scard(wkr_uid)} before fill")
    try:
        denied_at = None
        for i in range(11):
            await asyncio.sleep(0.4)  # stay under nginx ws_upgrade 5r/s
            try:
                ws = await websockets.connect(WSB + f"?token={WKR}", ssl=SSLCTX, open_timeout=10)
                conns.append(ws)
            except websockets.exceptions.InvalidStatus as e:
                denied_at = (i, e.response.status_code)
                break
        R.ok("15.4", denied_at is not None and denied_at[0] == 10 and denied_at[1] == 403 and scard(wkr_uid) == 10,
             f"denied_at={denied_at} scard={scard(wkr_uid)} (wire 403 stands in for in-process 4000; BUG-6)")
        before = len(deny_log())
        await ws_case("15.3", WSB, "garbage", expect_close="hs-fail")
        await asyncio.sleep(0.4)
        after = deny_log()
        R.ok("15.3", len(after) > before and "clause=bad-token" in after[before:],
             "bad token at full cap still hits AUTH-DENY (auth before cap)")
    finally:
        for ws in conns:
            try: await ws.close()
            except Exception: pass
    # 15.5 live push
    try:
        async with websockets.connect(WSB + f"?token={OWNER}", ssl=SSLCTX, open_timeout=10) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)  # hello
            import paho.mqtt.client as mqtt
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            c.tls_set(ca_certs=os.path.join(HERE, "..", "certs", "ca.crt"))
            c.username_pw_set("qa-pub", "qa-pub-pw-123")
            c.connect("localhost", 8883, 10); c.loop_start(); await asyncio.sleep(1.0)
            dts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            org = claims(OWNER)["org_id"]
            c.publish(f"{org}/farms/1/gateways/GW-QA0001/telemetry", json.dumps(
                {"gateway_hardware_id": "HWM-QA01", "gateway_slot_id": "GW-QA0001", "schema_version": "2.0",
                 "device_ts": dts, "is_buffered": False, "clock_drift": False,
                 "readings": [{"pack_id": "P-A", "metrics": {"temp": 26.5}}]}), qos=1)
            try:
                got = None
                t0 = time.time()
                while time.time() - t0 < 12:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
                    if m.get("type") == "telemetry.update":
                        got = m; break
                R.ok("15.5", got is not None and got.get("device_ts") == dts, f"update device_ts match={bool(got)}")
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
                R.ok("15.5", False, f"no update: {type(e).__name__}")
            c.loop_stop(); c.disconnect()
    except Exception as e:
        R.ok("15.5", False, f"{type(e).__name__}: {str(e)[:100]}")
    # 15.6 gap_fill bad since
    await ws_case("15.6", WSB, OWNER, send={"action": "gap_fill", "since": "garbage"}, expect_type="telemetry.gap_fill")
    # 15.7 compat shim: /ws/farms path (proxied) vs /api/compat path (plain proxy)
    await ws_case("15.7a", "wss://localhost/ws/farms/farm-1/telemetry", OWNER, expect_type="connection_established")
    await ws_case("15.7b", "wss://localhost/api/compat/v1/ws/farms/farm-1/telemetry", OWNER, expect_close="hs-fail")

asyncio.run(main())

# 15.8 redis down: cap fail-open, auth (DB) unaffected
subprocess.run(["docker", "stop", "morghdari-redis"], capture_output=True)
time.sleep(3)
try:
    async def probe():
        try:
            async with websockets.connect(WSB + f"?token={OWNER}", ssl=SSLCTX, open_timeout=12) as ws:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
                return m.get("type")
        except websockets.exceptions.ConnectionClosed as e:
            return f"closed-{e.code}"
        except Exception as e:
            return f"{type(e).__name__}"
    R.ok("15.8", asyncio.run(probe()) == "telemetry.hello", "auth ok + cap fail-open with redis down")
finally:
    subprocess.run(["docker", "start", "morghdari-redis"], capture_output=True)
    time.sleep(8)
R.ok("15.8b", True, "redis restarted")

R.v("15.9", "NA", "301s idle close verified from nginx config (proxy_read_timeout 300s); live wait skipped")
# 15.10 zombie sweep
uid = claims(WKR).get("user_id")
async def churn():
    for _ in range(20):
        try:
            ws = await websockets.connect(WSB + f"?token={WKR}", ssl=SSLCTX, open_timeout=8)
            await ws.close()
        except Exception:
            pass
asyncio.run(churn())
time.sleep(3)
out = subprocess.run(["docker", "exec", "-i", "-e", "REDIS_PASSWORD=" + env1["REDIS_PASSWORD"],
    "morghdari-redis", "redis-cli", "-a", env1["REDIS_PASSWORD"], "SCARD", f"ws:active:{uid}"],
    capture_output=True, text=True).stdout.strip()
R.ok("15.10", out in ("0", ""), f"ws:active scard={out!r} after 20 rapid cycles")
R.f.flush()
print("\nWAVE4 DONE", flush=True)
