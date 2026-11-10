import time
import threading
import paho.mqtt.client as mqtt

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18883

def test_shared_subscription_clean_session(clean_session: bool):
    tag = f"clean_session={clean_session}"
    topic = f"test/emqx551/shared/{'clean' if clean_session else 'persistent'}_{int(time.time())}"
    shared_topic = f"$share/telemetry_workers/{topic}"
    
    print(f"\n=======================================================")
    print(f"RUNNING TEST WITH {tag}")
    print(f"Topic: {topic}")
    print(f"Shared Topic: {shared_topic}")
    print(f"=======================================================")

    sub_a_received = []
    sub_b_received = []
    sub_a_reconnect_received = []

    sub_a_connected = threading.Event()
    sub_b_connected = threading.Event()

    # Subscriber A
    def on_connect_a(c, ud, flags, rc, props=None):
        print(f"[{tag}] Sub-A connected with rc={rc}")
        c.subscribe(shared_topic, qos=1)
        sub_a_connected.set()

    def on_message_a(c, ud, msg):
        print(f"[{tag}] Sub-A RECEIVED msg mid={msg.mid}, payload={msg.payload.decode()}")
        sub_a_received.append(msg)
        # Disconnect immediately WITHOUT acknowledging (manual_ack=True)
        print(f"[{tag}] Sub-A disconnecting WITHOUT sending PUBACK...")
        c.disconnect()

    client_id_a = f"worker-A-{'clean' if clean_session else 'pers'}-{int(time.time())}"
    sub_a = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id_a,
        clean_session=clean_session,
        protocol=mqtt.MQTTv311,
        manual_ack=True,
    )
    sub_a.on_connect = on_connect_a
    sub_a.on_message = on_message_a

    # Subscriber B
    def on_connect_b(c, ud, flags, rc, props=None):
        print(f"[{tag}] Sub-B connected with rc={rc}")
        c.subscribe(shared_topic, qos=1)
        sub_b_connected.set()

    def on_message_b(c, ud, msg):
        print(f"[{tag}] Sub-B RECEIVED msg mid={msg.mid}, payload={msg.payload.decode()}")
        sub_b_received.append(msg)
        c.ack(msg.mid, msg.qos)

    client_id_b = f"worker-B-{'clean' if clean_session else 'pers'}-{int(time.time())}"
    sub_b = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id_b,
        clean_session=clean_session,
        protocol=mqtt.MQTTv311,
        manual_ack=True,
    )
    sub_b.on_connect = on_connect_b
    sub_b.on_message = on_message_b

    # Start Sub A and Sub B
    sub_a.connect(BROKER_HOST, BROKER_PORT)
    sub_a.loop_start()
    sub_b.connect(BROKER_HOST, BROKER_PORT)
    sub_b.loop_start()

    sub_a_connected.wait(5.0)
    sub_b_connected.wait(5.0)
    time.sleep(1.0) # allow subscription to settle in EMQX

    # Publish QoS 1 message
    pub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"pub-{int(time.time())}")
    pub.connect(BROKER_HOST, BROKER_PORT)
    payload = f"test-payload-{tag}"
    print(f"[{tag}] Publishing QoS 1 message to {topic}...")
    pub.publish(topic, payload.encode(), qos=1)
    pub.disconnect()

    # Wait to see what happens
    print(f"[{tag}] Waiting up to 10s to see if Sub-B receives anything while Sub-A is offline...")
    t0 = time.time()
    sub_b_got_message = False
    while time.time() - t0 < 10.0:
        if len(sub_b_received) > 0:
            elapsed = time.time() - t0
            print(f"[{tag}] -> Sub-B received message after {elapsed:.2f}s!")
            sub_b_got_message = True
            break
        time.sleep(0.5)

    sub_a.loop_stop()

    if not sub_b_got_message:
        print(f"[{tag}] -> Sub-B DID NOT receive the message within 10s of Sub-A disconnecting!")
        if not clean_session:
            print(f"[{tag}] Testing reconnect of Sub-A with SAME client_id ({client_id_a})...")
            # Reconnect Sub-A with same client_id
            def on_reconnect_a(c, ud, flags, rc, props=None):
                print(f"[{tag}] Sub-A reconnected with rc={rc}")

            def on_reconnect_msg_a(c, ud, msg):
                print(f"[{tag}] Sub-A (reconnected) RECEIVED msg mid={msg.mid}, payload={msg.payload.decode()}")
                sub_a_reconnect_received.append(msg)
                c.ack(msg.mid, msg.qos)

            sub_a2 = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id_a,
                clean_session=False,
                protocol=mqtt.MQTTv311,
                manual_ack=True,
            )
            sub_a2.on_connect = on_reconnect_a
            sub_a2.on_message = on_reconnect_msg_a
            sub_a2.connect(BROKER_HOST, BROKER_PORT)
            sub_a2.loop_start()

            t1 = time.time()
            while time.time() - t1 < 5.0:
                if len(sub_a_reconnect_received) > 0:
                    print(f"[{tag}] -> Sub-A received the message upon reconnect!")
                    break
                time.sleep(0.5)
            sub_a2.loop_stop()
            sub_a2.disconnect()

    sub_b.loop_stop()
    sub_b.disconnect()

    print(f"[{tag}] SUMMARY:")
    print(f"  Sub-A initial received: {len(sub_a_received)}")
    print(f"  Sub-B received: {len(sub_b_received)}")
    print(f"  Sub-A reconnect received: {len(sub_a_reconnect_received)}")
    return {
        "clean_session": clean_session,
        "sub_a": len(sub_a_received),
        "sub_b": len(sub_b_received),
        "sub_a_reconnect": len(sub_a_reconnect_received)
    }

if __name__ == "__main__":
    res_false = test_shared_subscription_clean_session(clean_session=False)
    time.sleep(2)
    res_true = test_shared_subscription_clean_session(clean_session=True)
    print("\n\nFINAL COMPARISON ON EMQX 5.5.1:")
    print("clean_session=False:", res_false)
    print("clean_session=True:", res_true)
