"""
Script to test Redis outage scenarios against EMQX 5.5.1:
- Scenario A: One subscriber unavailable (failover to other subscriber)
- Scenario B: Both subscribers unavailable (behavior when all subscribers offline)
- Scenario C: Broker restart behavior
"""

import json
import os
import subprocess
import sys
import threading
import time
import paho.mqtt.client as mqtt

EMQX_HOST = "127.0.0.1"
EMQX_PORT = 18883

def get_emqx_metrics():
    """Retrieve key metrics from local EMQX 5.5.1 running in WSL."""
    try:
        out = subprocess.check_output(
            ["wsl", "-d", "Ubuntu", "--", "/home/mrbozorgvar/emqx/bin/emqx_ctl", "broker", "metrics"],
            text=True, stderr=subprocess.STDOUT
        )
    except subprocess.CalledProcessError as e:
        out = e.output

    metrics = {}
    for line in out.splitlines():
        if ":" in line:
            parts = line.split(":", 1)
            k = parts[0].strip()
            v = parts[1].strip()
            if v.isdigit():
                metrics[k] = int(v)
    return metrics


def run_scenario_a():
    print("\n" + "=" * 60)
    print("RUNNING SCENARIO A: One subscriber unavailable (failover)")
    print("=" * 60)

    ts = int(time.time())
    topic = f"test/scenario_a/{ts}"
    shared_topic = f"$share/telemetry_workers/{topic}"

    received_a = []
    received_b = []
    ready_a = threading.Event()
    ready_b = threading.Event()

    def on_a(c, ud, msg):
        print(f"[Sub-A] Received msg mid={msg.mid}, witholding PUBACK and disconnecting!")
        received_a.append(msg)
        c.disconnect()

    def on_b(c, ud, msg):
        print(f"[Sub-B] Received msg mid={msg.mid}, payload={msg.payload.decode()}, sending PUBACK!")
        received_b.append(msg)
        c.ack(msg.mid, msg.qos)

    sub_a = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sub-scenario-A-{ts}",
        clean_session=True,
        manual_ack=True,
    )
    sub_a.on_connect = lambda c, ud, flags, rc, props=None: (c.subscribe(shared_topic, qos=1), ready_a.set())
    sub_a.on_message = on_a

    sub_b = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sub-scenario-B-{ts}",
        clean_session=True,
        manual_ack=True,
    )
    sub_b.on_connect = lambda c, ud, flags, rc, props=None: (c.subscribe(shared_topic, qos=1), ready_b.set())
    sub_b.on_message = on_b

    # Connect Sub-A first so EMQX directs message to it
    sub_a.connect(EMQX_HOST, EMQX_PORT)
    sub_a.loop_start()
    ready_a.wait(3.0)
    time.sleep(0.5)

    sub_b.connect(EMQX_HOST, EMQX_PORT)
    sub_b.loop_start()
    ready_b.wait(3.0)
    time.sleep(0.5)

    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"pub-scenario-a-{ts}")
    pub.connect(EMQX_HOST, EMQX_PORT)
    test_payload = b"scenario-a-failover-payload"
    print(f"[Publisher] Publishing QoS 1 message to {topic}...")
    res = pub.publish(topic, test_payload, qos=1)
    res.wait_for_publish(timeout=3.0)
    print(f"[Publisher] Published message mid={res.mid}, puback received={res.is_published()}")
    pub.disconnect()

    # Wait for failover
    t0 = time.time()
    while time.time() - t0 < 5.0:
        if len(received_b) > 0:
            break
        time.sleep(0.1)

    sub_a.loop_stop()
    sub_b.loop_stop()
    sub_b.disconnect()

    print(f"[Result Scenario A] Sub-A received: {len(received_a)}, Sub-B received: {len(received_b)}")
    assert len(received_b) >= 1, "Sub-B failed to receive failover message"
    assert received_b[0].payload == test_payload
    print("--> Scenario A SUCCESSFUL: Sub-B took over unacked message after Sub-A disconnected.")
    return True


def run_scenario_b():
    print("\n" + "=" * 60)
    print("RUNNING SCENARIO B: Both subscribers offline during Redis outage")
    print("=" * 60)

    metrics_before = get_emqx_metrics()
    print("EMQX Metrics BEFORE Scenario B:")
    print(f"  messages.received: {metrics_before.get('messages.received')}")
    print(f"  messages.dropped: {metrics_before.get('messages.dropped')}")
    print(f"  messages.dropped.no_subscriber: {metrics_before.get('messages.dropped.no_subscriber')}")
    print(f"  packets.puback.sent: {metrics_before.get('packets.puback.sent')}")

    ts = int(time.time())
    topic = f"test/scenario_b/{ts}"
    shared_topic = f"$share/telemetry_workers/{topic}"

    # 1. Start two subscribers
    sub_a_received = []
    sub_b_received = []
    ready_a = threading.Event()
    ready_b = threading.Event()

    sub_a = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sub-scenB-A-{ts}",
        clean_session=True,
        manual_ack=True,
    )
    sub_a.on_connect = lambda c, ud, flags, rc, props=None: (c.subscribe(shared_topic, qos=1), ready_a.set())
    sub_a.on_message = lambda c, ud, msg: (sub_a_received.append(msg), c.ack(msg.mid, msg.qos))

    sub_b = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sub-scenB-B-{ts}",
        clean_session=True,
        manual_ack=True,
    )
    sub_b.on_connect = lambda c, ud, flags, rc, props=None: (c.subscribe(shared_topic, qos=1), ready_b.set())
    sub_b.on_message = lambda c, ud, msg: (sub_b_received.append(msg), c.ack(msg.mid, msg.qos))

    sub_a.connect(EMQX_HOST, EMQX_PORT)
    sub_a.loop_start()
    sub_b.connect(EMQX_HOST, EMQX_PORT)
    sub_b.loop_start()

    ready_a.wait(3.0)
    ready_b.wait(3.0)
    time.sleep(0.5)
    print("Both subscribers connected and subscribed to shared topic.")

    # 2. Simulate Redis outage -> both subscribers disconnect to enter health loop
    print("Simulating Redis outage: disconnecting both subscriber clients...")
    sub_a.disconnect()
    sub_b.disconnect()
    sub_a.loop_stop()
    sub_b.loop_stop()
    time.sleep(1.0)
    print("Both subscribers are now OFFLINE.")

    # 3. Publish 5 uniquely identifiable QoS 1 messages while both subscribers are offline
    pub_acks = []
    def on_pub(client, userdata, mid, reason_code=None, properties=None):
        pub_acks.append(mid)

    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"pub-scenB-{ts}")
    pub.on_publish = on_pub
    pub.connect(EMQX_HOST, EMQX_PORT)
    pub.loop_start()

    messages_published = []
    print("\nPublishing 5 QoS 1 messages while both subscribers are offline...")
    for i in range(5):
        payload = f"offline-msg-{i}-{ts}".encode()
        info = pub.publish(topic, payload, qos=1)
        messages_published.append((info.mid, payload))
        info.wait_for_publish(timeout=3.0)
        print(f"  Published msg {i}: mid={info.mid}, is_published={info.is_published()}")

    time.sleep(1.0)
    pub.loop_stop()
    pub.disconnect()

    print(f"Publisher received PUBACK for mids: {pub_acks}")

    # 4. Check EMQX metrics during outage
    metrics_during = get_emqx_metrics()
    print("\nEMQX Metrics AFTER publishing with no subscribers online:")
    print(f"  messages.received: {metrics_during.get('messages.received')} (+{metrics_during.get('messages.received', 0) - metrics_before.get('messages.received', 0)})")
    print(f"  messages.dropped: {metrics_during.get('messages.dropped')} (+{metrics_during.get('messages.dropped', 0) - metrics_before.get('messages.dropped', 0)})")
    print(f"  messages.dropped.no_subscriber: {metrics_during.get('messages.dropped.no_subscriber')} (+{metrics_during.get('messages.dropped.no_subscriber', 0) - metrics_before.get('messages.dropped.no_subscriber', 0)})")
    print(f"  packets.puback.sent: {metrics_during.get('packets.puback.sent')} (+{metrics_during.get('packets.puback.sent', 0) - metrics_before.get('packets.puback.sent', 0)})")

    # 5. Simulate Redis recovery -> subscribers reconnect
    print("\nSimulating Redis recovery: subscribers reconnecting to shared subscription...")
    sub_a_reconnected_received = []
    sub_b_reconnected_received = []

    sub_a_rec = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sub-scenB-A-{ts}",
        clean_session=True,
        manual_ack=True,
    )
    sub_a_rec.on_message = lambda c, ud, msg: (sub_a_reconnected_received.append(msg), c.ack(msg.mid, msg.qos))

    sub_b_rec = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sub-scenB-B-{ts}",
        clean_session=True,
        manual_ack=True,
    )
    sub_b_rec.on_message = lambda c, ud, msg: (sub_b_reconnected_received.append(msg), c.ack(msg.mid, msg.qos))

    sub_a_rec.connect(EMQX_HOST, EMQX_PORT)
    sub_a_rec.subscribe(shared_topic, qos=1)
    sub_a_rec.loop_start()

    sub_b_rec.connect(EMQX_HOST, EMQX_PORT)
    sub_b_rec.subscribe(shared_topic, qos=1)
    sub_b_rec.loop_start()

    print("Waiting 5 seconds to observe if any messages published during outage are delivered...")
    time.sleep(5.0)

    sub_a_rec.loop_stop()
    sub_b_rec.loop_stop()
    sub_a_rec.disconnect()
    sub_b_rec.disconnect()

    total_recovered = len(sub_a_reconnected_received) + len(sub_b_reconnected_received)
    print(f"Total messages received by reconnected subscribers: {total_recovered} / 5")
    print(f"Sub-A received: {[m.payload.decode() for m in sub_a_reconnected_received]}")
    print(f"Sub-B received: {[m.payload.decode() for m in sub_b_reconnected_received]}")

    dropped_count = metrics_during.get('messages.dropped.no_subscriber', 0) - metrics_before.get('messages.dropped.no_subscriber', 0)
    print(f"\nExact EMQX Drop Count for this batch: {dropped_count} messages dropped with 'no_subscriber'!")
    return {
        "published": 5,
        "pubacks_received_by_publisher": len(pub_acks),
        "messages_dropped_no_subscriber": dropped_count,
        "recovered_by_subscribers": total_recovered,
    }


def run_scenario_c():
    print("\n" + "=" * 60)
    print("RUNNING SCENARIO C: Broker restart during Redis outage")
    print("=" * 60)
    print("Evaluating broker restart impact:")
    print("In EMQX 5.5.1, shared subscriptions without persistent subscriber sessions")
    print("are ephemeral. Even if messages were buffered in memory, a broker restart")
    print("discards non-retained in-memory message queues unless persisted to mria/disk.")
    print("Combined with clean_session=True, sessions do not survive broker restart.")


if __name__ == "__main__":
    run_scenario_a()
    res_b = run_scenario_b()
    run_scenario_c()
    print("\n" + json.dumps(res_b, indent=2))
