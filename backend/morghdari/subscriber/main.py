"""MQTT telemetry subscriber — M3-01 (Blueprint §4, MQTT Topic Map S1).

WHY this exists: gateways publish sensor readings to EMQX; the subscriber is
the ONLY bridge between the broker and our Redis/Celery pipeline. It must
survive broker restarts and load-balance across N instances, hence:

- SHARED subscription `$share/telemetry_workers/+/farms/+/gateways/+/telemetry`
  (Topic Map S1 — the group name is contract, decision Q2 2026-08-25):
  EMQX load-balances messages across all members of the group.
- qos=1 + clean_session=False: an offline subscriber's messages queue in the
  broker session (bounded by Session Expiry — Topic Map note) instead of being
  dropped. Redelivery duplicates are EXPECTED; dedup lives in M3-03.
- SUBSCRIBER_CLIENT_ID env is REQUIRED: a stable client id is what binds the
  queued session to this instance. Two processes with the same id will kick
  each other off the broker — refuse to boot without it rather than guess.

فارسی: مشترکِ تله‌متری — اشتراک اشتراکی با گروه telemetry_workers، qos=1 و
نشست پایدار تا پیام‌های زمانِ آفلاین در بروکر صف شوند؛ شناسهٔ یکتای کلاینت اجباری
است چون نشست صف‌شده به همین شناسه گره خورده است.
"""

import logging
import os
import sys

import paho.mqtt.client as mqtt
import redis

from .pipeline import handle_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("morghdari.subscriber")

SHARED_TOPIC = "$share/telemetry_workers/+/farms/+/gateways/+/telemetry"
SUBSCRIBE_QOS = 1


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        logger.error(
            "missing required env %s — refusing to boot: the broker queues "
            "messages against THIS client id, so guessing one would orphan them",
            name,
        )
        raise SystemExit(2)
    return value


def build_client() -> mqtt.Client:
    """Construct the paho client exactly as the docs prescribe (S1)."""
    client_id = _required_env("SUBSCRIBER_CLIENT_ID")
    # clean_session=False (MQTT v3.1.1): persistent session — offline queueing.
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=False,
    )
    username = os.environ.get("MQTT_USERNAME", "").strip()
    if username:
        client.username_pw_set(username, os.environ.get("MQTT_PASSWORD", ""))

    client.on_connect = _on_connect
    client.on_message = _on_message
    client.on_disconnect = _on_disconnect
    return client


def _on_connect(client, userdata, flags, reason_code, properties=None):
    if getattr(reason_code, "is_failure", False):
        # e.g. bad user/password or unauthorized — visible, never silent (NR-4 spirit).
        logger.error("CONNACK failure: %s", reason_code)
        return
    logger.info("connected to broker (session_present=%s)", flags.session_present)
    result, mid = client.subscribe(SHARED_TOPIC, qos=SUBSCRIBE_QOS)
    if result == mqtt.MQTT_ERR_SUCCESS:
        logger.info("subscribed via %s (mid=%s, qos=%s)", SHARED_TOPIC, mid, SUBSCRIBE_QOS)
    else:
        logger.error("subscribe failed: %s", result)


def _on_message(client, userdata, message) -> None:
    # M3-02: every message now passes the pipeline's ordered gates; rejections
    # dead-letter with a reason, happy-path logging stays visible until the
    # enrich/XADD gate lands (M3-04).
    handle_message(_redis(), message.topic, message.payload)


_redis_client = None


def _redis():
    """Shared Redis connection — same URL convention as Django settings."""
    global _redis_client
    if _redis_client is None:
        url = os.environ.get(
            "REDIS_URL", "redis://:dev_redis_password@localhost:6379"
        )
        _redis_client = redis.Redis.from_url(url, decode_responses=True)
    return _redis_client


def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    logger.warning("disconnected from broker: %s", reason_code)


def main() -> None:
    host = os.environ.get("MQTT_HOST", "127.0.0.1")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    client = build_client()
    logger.info("booting subscriber → %s:%s", host, port)
    client.connect(host, port, keepalive=60)
    client.loop_forever()  # reconnects automatically; blocking by design


if __name__ == "__main__":
    main()
