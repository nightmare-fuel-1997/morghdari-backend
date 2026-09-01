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

import json
import logging
import os
import sys
import time

import paho.mqtt.client as mqtt
import redis

# The bridge now resolves slots through the ORM (db_fetch), so it needs the
# same settings convention as Celery (production compose sets the real module).
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

from .pipeline import handle_message

from apps.alerts.services.redis_keys import pack_lwt_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("morghdari.subscriber")

SHARED_TOPIC = "$share/telemetry_workers/+/farms/+/gateways/+/telemetry"
# Pack-liveness status topic: retained 'online' / Last-Will 'offline' from
# the simulator's per-pack clients. Subscribed plainly (not in the share
# group) — the writes are idempotent, so every subscriber mirroring them
# is harmless and a lone instance never loses the status feed.
STATUS_TOPIC = "+/farms/+/gateways/+/status"
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
    status_result, status_mid = client.subscribe(STATUS_TOPIC, qos=SUBSCRIBE_QOS)
    if status_result == mqtt.MQTT_ERR_SUCCESS:
        logger.info("subscribed to %s (mid=%s)", STATUS_TOPIC, status_mid)
    else:
        logger.error("status subscribe failed: %s", status_result)


def _resolve_slot(org_id, hardware_id, pack_id):
    """db_fetch for the pipeline: (org, hardware_id, pack_id) -> slot_id or None.

    Lazy Django ORM inside the tenant GUC (same pattern as consumers.py) so
    FORCE RLS sees the org; without this wiring every publish dead-letters
    UNKNOWN_SLOT on the real MQTT path. Never raises — a DB hiccup must not
    crash the bridge (a miss just dead-letters honestly).
    فارسی: پیدا کردن اسلات از دیتابیس با زمینهٔ سازمان؛ خطا یعنی پیام صادقانه
    به صندوق مرده می‌رود، نه کرشِ بریج.
    """
    import django

    django.setup()  # idempotent
    from django.db import transaction

    from apps.devices.models import GatewaySensorMapping
    from apps.tenants.context import tenant_context

    try:
        with transaction.atomic(), tenant_context(org_id):
            return (
                GatewaySensorMapping.objects.filter(
                    org_id=org_id,
                    gateway_hardware_id=hardware_id,
                    pack_id=pack_id,
                )
                .values_list("slot_id", flat=True)
                .first()
            )
    except Exception:
        logger.exception("slot resolution failed for %s/%s", hardware_id, pack_id)
        return None


def _resolve_status_saloon(org_id: str, slot_id: str):
    """slot_id -> saloon id for the lwt key (status topics carry no saloon
    segment). Same RLS-scoped ORM pattern as _resolve_slot: unknown slots are
    simply ignored, never crash the bridge.
    فارسی: شناسهٔ سالن از روی اسلات برای کلیدِ وضعیت پک; اسلات ناشناخته نادیده گرفته شود."""
    import django

    django.setup()  # idempotent
    from django.db import transaction

    from apps.devices.models import SaloonSensorSlot
    from apps.tenants.context import tenant_context

    try:
        with transaction.atomic(), tenant_context(org_id):
            return (
                SaloonSensorSlot.objects.filter(org_id=org_id, slot_id=slot_id)
                .values_list("saloon_id", flat=True)
                .first()
            )
    except Exception:
        logger.exception("status slot resolution failed for %s/%s", org_id, slot_id)
        return None


def _handle_status(client, topic: str, payload: bytes) -> None:
    """Mirror a pack's retained status into Redis (M-liveness bridge).

    The simulator's per-pack clients publish retained 'online' on connect and
    their Last Will flips the retained topic to 'offline' on unclean death.
    This handler stores the last word as sensor:lwt:{saloon}:{slot} (NO TTL
    — the retained MQTT state is the durable truth), so the boss board sees
    a dead pack immediately instead of waiting for the sensor-status TTL.
    Malformed payloads and unknown slots are ignored (never crash the bridge).
    فارسی: آخرین وضعیت پک روی کلیدِ ردیس مینشیند تا داشبورد فوراً ببیند.
    """
    if payload is None:
        return
    parts = topic.split("/")
    if len(parts) != 6 or parts[1] != "farms" or parts[3] != "gateways" or parts[5] != "status":
        logger.debug("ignoring non-conformant status topic %s", topic)
        return
    org_id, slot_id = parts[0], parts[4]
    try:
        data = json.loads(payload)
    except ValueError:
        logger.debug("ignoring malformed status payload on %s", topic)
        return
    if not isinstance(data, dict):
        return
    status = data.get("status")
    if status not in ("online", "offline"):
        return  # unknown status word — leave the last known state untouched
    saloon_id = _resolve_status_saloon(org_id, slot_id)
    if saloon_id is None:
        logger.debug("status for unknown slot %s/%s ignored", org_id, slot_id)
        return
    client.set(pack_lwt_key(saloon_id, slot_id), status)
    if status == "offline":
        logger.warning("pack offline (LWT): saloon %s slot %s", saloon_id, slot_id)
    else:
        logger.debug("pack online: saloon %s slot %s", saloon_id, slot_id)


def _on_message(client, userdata, message) -> None:
    if message.topic.endswith("/status"):
        # Pack-liveness lane: retained status -> Redis lwt key, not telemetry.
        _handle_status(_redis(), message.topic, message.payload)
        return
    # M3-02: every message now passes the pipeline's ordered gates; rejections
    # dead-letter with a reason, happy-path logging stays visible until the
    # enrich/XADD gate lands (M3-04).
    handle_message(
        _redis(),
        message.topic,
        message.payload,
        db_fetch=_resolve_slot,
    )


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
    # Boot-time broker outage must not kill the bridge: paho only reconnects
    # after a SUCCESSFUL first connect, so retry that first connect with a
    # fixed beat (broker comes up as part of the stack, usually within seconds).
    while True:
        try:
            client.connect(host, port, keepalive=60)
            break
        except OSError as exc:
            logger.warning("broker unreachable (%s); retrying in 2s", exc)
            time.sleep(2)
    client.loop_forever()  # reconnects automatically; blocking by design


if __name__ == "__main__":
    main()
