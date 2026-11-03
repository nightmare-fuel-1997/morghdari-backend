"""Morghdari plaintext-MQTT test — software team, 2026-11-03.

Connects to the broker, subscribes to the test unit's reported lane,
publishes a JSON report, and expects to receive the identical message
back (self-echo). Prints PASS/FAIL and every step.

Usage:
    pip install paho-mqtt
    python mqtt_test.py
"""
import time
import warnings

import paho.mqtt.client as mqtt

warnings.filterwarnings("ignore")

HOST = "82.115.17.252"
PORT = 1883
USER = "GW-TEST-B784019A"
PASSWORD = "JfnKx5wUbEYTzKDUBnQ8K2L6"  # 24 chars, rotated 2026-11-03 16:21
SLOT = USER
TOPIC = f"devices/{SLOT}/reported"

log = []


def stamp():
    return time.strftime("%H:%M:%S")


def main():
    got = []

    c = mqtt.Client(client_id="morghdari-test-" + str(int(time.time())),
                    protocol=mqtt.MQTTv311)
    c.username_pw_set(USER, PASSWORD)
    c.on_connect = lambda cl, u, f, rc, *a: log.append(
        f"[{stamp()}] CONNECT rc={rc}" + ("  AUTH OK" if rc == 0 else "  AUTH FAILED"))
    c.on_subscribe = lambda cl, u, mid, rc, *a: log.append(
        f"[{stamp()}] SUBACK granted QoS={rc}")
    c.on_message = lambda cl, u, m: got.append(m) or log.append(
        f"[{stamp()}] MESSAGE RECEIVED topic={m.topic}\n"
        f"           payload={m.payload.decode()}")

    log.append(f"[{stamp()}] connecting to {HOST}:{PORT} as {USER} ...")
    c.connect(HOST, PORT)
    c.loop_start()
    time.sleep(2)

    c.subscribe(TOPIC, qos=1)
    time.sleep(1)
    log.append(f"[{stamp()}] subscribed to {TOPIC}")

    payload = ('{"schema_version":"1.0","slot_id":"' + SLOT
               + '","state":"ONLINE","source":"sw-team-test","ts":"'
               + stamp() + '"}')
    info = c.publish(TOPIC, payload, qos=1)
    info.wait_for_publish(5)
    log.append(f"[{stamp()}] PUBLISHED to {TOPIC}")

    time.sleep(3)
    c.disconnect()
    c.loop_stop()

    echo_ok = any(m.payload.decode() == payload for m in got)
    log.append(f"[{stamp()}] DISCONNECTED")

    print("\n".join(log))
    print()
    if echo_ok:
        print("=== ECHO TEST: PASSED — pub/sub works end to end ===")
    else:
        print("=== ECHO TEST: FAILED — published but no identical echo ===")
        if not got:
            print("(no message arrived at all — check SUBACK above; "
                  "rc=128 means the topic was denied)")


if __name__ == "__main__":
    main()
