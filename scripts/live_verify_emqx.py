"""Live M5-07/M5-08 verification against the real EMQX 5.5 broker.

Drives the EXACT broker calls the backend makes (mqtt_auth/services.py +
emqx_provision.py) and proves, over MQTT itself:

  1. AUTHN: a gateway user created with PLAINTEXT (the EMQX 5.5 built-in-DB
     contract — pre-hashed password_hash is rejected) can connect, and a
     wrong password cannot.
  2. AUTHZ allow: with the pinned telemetry rule, the gateway's publish to
     its own topic is DELIVERED to a subscriber.
  3. AUTHZ revoke (suspension): with rules emptied (no_match=deny), the same
     publish is DENIED — the subscriber does NOT receive it.
  4. ROTATION: after the rotation PUT, the NEW password connects and the
     OLD one is rejected (the rotation commit's broker step).

Usage (dev stack running, emqx_provision.py applied):
    python scripts/live_verify_emqx.py   (EMQX on 127.0.0.1:1883, API on 18083)

Endpoint overrides (env) — used by the docker-compose.ci.yml `live-verify`
job, which runs this script from inside the compose network against the
`emqx` service (broker semantics must never be only mocked):
    EMQX_API_URL (default http://127.0.0.1:18083)
    EMQX_API_USER / EMQX_API_PASS (default demo-admin / dev_password)
    EMQX_MQTT_HOST / EMQX_MQTT_PORT (default 127.0.0.1 / 1883)

Result 2026-09-02 (EMQX 5.5.1): 10/10 checks passed — the broker-side
semantics of M5-07 (suspended gateway cannot publish) and M5-08 (rotation
with old-password rejection) are confirmed against a REAL broker, which CI
can only mock. Re-run before recording the M5-10 WALKTHROUGH.
"""

import base64
import json
import os
import queue
import sys
import time
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt

API = os.environ.get("EMQX_API_URL", "http://127.0.0.1:18083")
API_USER = os.environ.get("EMQX_API_USER", "demo-admin")
API_PASS = os.environ.get("EMQX_API_PASS", "dev_password")
BROKER = (os.environ.get("EMQX_MQTT_HOST", "127.0.0.1"),
          int(os.environ.get("EMQX_MQTT_PORT", "1883")))
CHAIN = "password_based:built_in_database"
ORG, FARM = "liveorg", 1
GATEWAY = "GW-LIVE-1"
TOPIC = f"{ORG}/farms/{FARM}/gateways/{GATEWAY}/telemetry"
SUB_TOPIC = "+/farms/+/gateways/+/telemetry"

fails = []


def api(method, path, body=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    token = base64.b64encode(f"{API_USER}:{API_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode(errors="replace") if raw else None
        return exc.code, payload


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not ok:
        fails.append(name)
    return ok


def rc_ok(rc):
    return rc is not None and getattr(rc, "value", rc) == 0


def mqtt_connect(client_id, username, password):
    """Connect with credentials; return (connected_ok, rc)."""
    result = {}

    def on_connect(client, userdata, flags, rc, props=None):
        result["rc"] = rc

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    c.username_pw_set(username, password)
    c.on_connect = on_connect
    try:
        c.connect(BROKER[0], BROKER[1], keepalive=10)
        c.loop_start()
        deadline = time.time() + 6
        while "rc" not in result and time.time() < deadline:
            time.sleep(0.1)
        c.loop_stop()
        c.disconnect()
    except Exception as exc:  # noqa: BLE001
        return False, f"exception {exc}"
    got = result.get("rc")
    return rc_ok(got), got


def publish_and_wait(client_id, password, topic, payload, subscriber_q, wait_s=4.0):
    """Publish as the gateway user; return whether the subscriber got it."""
    sent = {}

    def on_publish(client, userdata, mid, rc=None, props=None):
        sent["mid"] = mid

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    c.username_pw_set(client_id, password)
    c.on_publish = on_publish
    c.connect(BROKER[0], BROKER[1], keepalive=10)
    c.loop_start()
    c.publish(topic, payload, qos=1)
    deadline = time.time() + wait_s
    while time.time() < deadline and not sent:
        time.sleep(0.05)
    c.loop_stop()
    c.disconnect()
    # Did the subscriber receive it? Drain its queue.
    got = None
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            got = subscriber_q.get(timeout=0.2)
            break
        except queue.Empty:
            continue
    return got is not None, got


def main():
    # ---- 1. Authn user creation (backend's ensure_gateway_user) ------------
    st, payload = api("POST", f"/api/v5/authentication/{CHAIN}/users",
                      {"user_id": GATEWAY, "password": "live-pass-1"})
    check("gateway user created with plaintext", st in (200, 201), f"(HTTP {st})")

    ok, rc = mqtt_connect(f"{GATEWAY}-wrong", GATEWAY, "live-pass-WRONG")
    check("wrong password rejected at CONNECT", not ok, f"(rc={rc})")

    # ---- 2. Allow rule (backend's sync_gateway_acls) ------------------------
    st, payload = api("PUT", f"/api/v5/authorization/sources/built_in_database/rules/users/{GATEWAY}",
                      {"username": GATEWAY, "rules": [
                          {"permission": "allow", "action": "publish", "topic": TOPIC},
                      ]})
    check("allow rule pushed", st in (200, 201, 204), f"(HTTP {st})")

    ok, rc = mqtt_connect(GATEWAY, GATEWAY, "live-pass-1")
    check("gateway connects with issued password", ok, f"(rc={rc})")

    # subscriber on the shared lane
    received = queue.Queue()

    def on_message(client, userdata, msg):
        received.put(msg.payload.decode())

    sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="live-verify-sub")
    sub.username_pw_set("demo-sub", "devpass")
    sub.on_message = on_message
    sub.connect(BROKER[0], BROKER[1], keepalive=10)
    sub.loop_start()
    sub.subscribe(SUB_TOPIC, qos=1)
    time.sleep(1.0)  # let the subscription land

    delivered, got = publish_and_wait(GATEWAY, "live-pass-1", TOPIC, '{"v":1}', received)
    check("allowed publish DELIVERED to subscriber", delivered,
          f"(topic={TOPIC}, got={got!r})")

    # ---- 3. Revocation (backend's revoke_gateway_acls) ----------------------
    st, payload = api("PUT", f"/api/v5/authorization/sources/built_in_database/rules/users/{GATEWAY}",
                      {"username": GATEWAY, "rules": []})
    check("revoke empties rules", st in (200, 201, 204), f"(HTTP {st})")

    time.sleep(2.0)  # allow the broker's ACL result cache TTL to lapse
    denied, got = publish_and_wait(GATEWAY, "live-pass-1", TOPIC, '{"v":2}', received, wait_s=5.0)
    check("revoked publish NOT delivered (no_match=deny)", not denied,
          f"(got={got!r})")

    # ---- 4. Rotation (backend's update_gateway_user_password) --------------
    st, payload = api("PUT", f"/api/v5/authentication/{CHAIN}/users/{GATEWAY}",
                      {"password": "live-pass-2"})
    check("rotation password pushed", st == 200, f"(HTTP {st})")

    ok, rc = mqtt_connect(f"{GATEWAY}-new", GATEWAY, "live-pass-2")
    check("NEW password connects after rotation", ok, f"(rc={rc})")
    ok, rc = mqtt_connect(f"{GATEWAY}-old", GATEWAY, "live-pass-1")
    check("OLD password rejected after rotation", not ok, f"(rc={rc})")

    sub.loop_stop()
    sub.disconnect()

    # restore allow rule + drop the test user so a future live run starts clean
    api("PUT", f"/api/v5/authorization/sources/built_in_database/rules/users/{GATEWAY}",
        {"username": GATEWAY, "rules": [
            {"permission": "allow", "action": "publish", "topic": TOPIC}]})
    api("DELETE", f"/api/v5/authentication/{CHAIN}/users/{GATEWAY}")

    print()
    if fails:
        print(f"LIVE VERIFICATION FAILED: {len(fails)} check(s): {fails}")
        return 1
    print("LIVE VERIFICATION PASSED — M5-07 authn/authz/revoke + M5-08 rotation "
          "confirmed against real EMQX 5.5")
    return 0


if __name__ == "__main__":
    sys.exit(main())