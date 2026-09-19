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

import http.server
import logging
import os
import sys
import threading
import time
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
import redis

from .pipeline import handle_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("morghdari.subscriber")

SHARED_TOPIC = "$share/telemetry_workers/+/farms/+/gateways/+/telemetry"
STATUS_TOPIC = "+/farms/+/gateways/+/status"
SUBSCRIBE_QOS = 1

# v0.7 M7-10 live-drill fix: a denied SUBACK (granted 0x80) used to be
# invisible — main() logged "subscribed" the moment the SUBSCRIBE packet was
# QUEUED, never checked the broker's verdict, and never retried. A broker
# restart race (EMQX still initializing its authz pool when the subscriber
# boots) could therefore leave ingestion silently dead: PUBs ACK'd, no
# matching subscription, nothing in any stream, no DLQ, no log line. Now the
# SUBACK verdict is checked per topic; a denied lane is CRITICAL-logged and
# resubscribed with backoff until the broker grants it.
#
# فارسی: بررسی رأی SUBACK — اگر بروکر اشتراک را رد کند (۰x۸۰) دیگر سکوت
# نمی‌کنیم؛ خطای CRITICAL می‌نویسیم و با فاصلهٔ زمانی دوباره سابسکرایب
# می‌کنیم تا ingestion بی‌صدا نمیرد.
_SUBACK_DENIED = 0x80
_SUBSCRIBE_RETRY_S = 5
_pending_subs: dict = {}  # mid -> topic (the SUBACK only carries the mid)
_granted_topics: set = set()  # topics the broker has currently granted

# M7 (v0.7): the /metrics surface Observability §3.2 promises — subscriber_up
# drives the SubscriberTierDegraded page (monitoring/alerts.yml).
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9091"))
# True message counter — bumped ONLY by _on_message (a /metrics scrape must
# never move the metric it reports; rehearsal fix M7-06).
_messages_seen = 0
_messages_lock = threading.Lock()


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


def _apply_tls(client) -> None:
    """M7 (v0.7): the production listener is TLS-ONLY (8883, emqx.conf) — the
    subscriber MUST speak TLS or the broker drops every handshake.

    MQTT_TLS=1 enables. MQTT_TLS_CA names a CA bundle (unset = the system
    trust store — the VPS-day Let's Encrypt path). MQTT_TLS_INSECURE=1 relaxes
    hostname verification: accepted-risk for the INTERNAL subscriber→emqx leg
    (2026-09-19 review — hostname "emqx" is in no public SAN; traffic never
    leaves the docker bridge; encryption still defeats passive sniffing and
    broker auth/ACLs still apply). External gateways verify normally.
    """
    if os.environ.get("MQTT_TLS", "").strip().lower() not in {"1", "true", "yes"}:
        return
    import ssl

    ca = os.environ.get("MQTT_TLS_CA", "").strip() or None
    context = ssl.create_default_context(cafile=ca)
    if os.environ.get("MQTT_TLS_INSECURE", "").strip().lower() in {"1", "true", "yes"}:
        logger.warning("MQTT_TLS_INSECURE=1 — hostname verification OFF (staging only!)")
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    client.tls_set_context(context)
    logger.info("TLS enabled (ca=%s)", ca or "<system trust store>")


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
    _apply_tls(client)

    client.on_connect = _on_connect
    client.on_subscribe = _on_subscribe
    client.on_message = _on_message
    client.on_disconnect = _on_disconnect
    return client


def _request_subscriptions(client) -> None:
    """(Re)send both subscribes; the verdict lands in _on_subscribe.

    Called on EVERY connect (paho does not resubscribe by itself) and again
    after any denied SUBACK. The mid->topic map lets the SUBACK handler name
    the lane instead of logging a bare mid.
    """
    for topic in (SHARED_TOPIC, STATUS_TOPIC):
        result, mid = client.subscribe(topic, qos=SUBSCRIBE_QOS)
        if result == mqtt.MQTT_ERR_SUCCESS:
            _pending_subs[mid] = topic
            logger.debug("subscribe requested: %s (mid=%s)", topic, mid)
        else:
            logger.error("subscribe request failed locally: %s (%s)", topic, result)


def _on_subscribe(client, userdata, mid, granted_qos, properties=None):
    """SUBACK verdict — the check the boot path never had.

    Granted: log the lane (this is the ONLY "subscribed" log that means the
    broker holds the subscription). Denied (0x80): ingestion is DOWN on this
    lane — CRITICAL-log it and resubscribe with backoff until granted.
    """
    topic = _pending_subs.pop(mid, "<unknown>")
    codes = [getattr(q, "value", q) for q in granted_qos]
    if any(code == _SUBACK_DENIED for code in codes):
        _granted_topics.discard(topic)
        logger.critical(
            "SUBACK DENIED for %s (mid=%s, granted=%s) — ingestion is DOWN "
            "on this lane; resubscribing in %ss",
            topic, mid, codes, _SUBSCRIBE_RETRY_S,
        )
        timer = threading.Timer(_SUBSCRIBE_RETRY_S, _request_subscriptions, args=(client,))
        timer.daemon = True
        timer.start()
        return
    _granted_topics.add(topic)
    if topic == STATUS_TOPIC:
        logger.info("subscribed to %s (granted qos=%s)", topic, codes)
    else:
        logger.info("subscribed via %s (mid=%s, granted qos=%s)", topic, mid, codes)


def _subscriptions_ok() -> bool:
    """Both lanes currently granted — drives the subscriber_subscribed gauge."""
    return {SHARED_TOPIC, STATUS_TOPIC} <= _granted_topics


def _on_connect(client, userdata, flags, reason_code, properties=None):
    if getattr(reason_code, "is_failure", False):
        # e.g. bad user/password or unauthorized — visible, never silent (NR-4 spirit).
        logger.error("CONNACK failure: %s", reason_code)
        return
    logger.info("connected to broker (session_present=%s)", flags.session_present)
    _request_subscriptions(client)


_django_configured = False


def _django_ready() -> bool:
    """True once Django is configured for ORM lookups (lazy, once).

    The resolver docstring's contract: the subscriber wires db_fetch to the
    Django ORM "at integration time" — which requires django.setup(). The
    prod compose provides DATABASE_URL + DJANGO_SETTINGS_MODULE for the
    subscriber services (the docs compose never did — rehearsal finding);
    a pure-MQTT process without them keeps legacy no-record behavior.
    """
    global _django_configured
    if _django_configured:
        return True
    try:
        import django
        from django.conf import settings

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
        if not settings.configured:
            django.setup()
        _django_configured = django.apps.apps.ready
        return _django_configured
    except Exception as exc:
        logger.warning("Django ORM unavailable for slot resolution (%s)", exc)
        return False


def _slot_db_fetch(org_id: str, hardware_id: str, pack_id: str):
    """M3-05+ integration the resolver docstring promises: wire the pack →
    slot lookup to the Django ORM.

    v0.7 M7-06 rehearsal fix (RLS era): gateway_sensor_mapping is org-scoped
    and FORCE-RLS'd — a bare read sees ZERO rows, so every real telemetry
    message dead-lettered UNKNOWN_SLOT on the rehearsal stack (the demo
    console always injected its own db_fetch and masked this). The topic's
    org is verified against the mapping row's org on purpose: a mapping row
    outside the topic's org is a mis-provision and must NOT resolve.
    """
    if not _django_ready():
        return None
    try:
        from django.db import connection, transaction

        from apps.devices.models import GatewaySensorMapping

        def _lookup():
            return (
                GatewaySensorMapping.objects.filter(
                    gateway_hardware_id=hardware_id, pack_id=pack_id
                )
                .values_list("slot_id", flat=True)
                .first()
            )

        if connection.vendor == "postgresql":
            from apps.tenants.context import tenant_context

            with transaction.atomic(), tenant_context(org_id):
                return _lookup()
        return _lookup()
    except Exception:
        logger.exception("slot resolution failed for %s/%s", hardware_id, pack_id)
        return None


def _status_fetch(org_id: str, gateway_slot: str):
    """M5-04: the gateway's provisioning status from Django, lazily — a
    pure-MQTT process without Django keeps legacy behavior (no record ⇒
    telemetry flows).

    v0.7 M7-06 rehearsal fix (RLS era): gateway_slots is FORCE-RLS'd, so the
    read MUST run inside tenant_context — and the org comes from the TOPIC
    (the ACL pins every gateway to publish only under its own org's topic
    prefix, so the topic org is unforgeable org evidence). A bare read would
    see zero rows and silently route a PENDING_APPROVAL gateway's telemetry
    as if it were approved — a trust-boundary regression, not a paper cut.
    """
    if not _django_ready():
        return None
    try:
        from django.db import connection, transaction

        from apps.devices.models import GatewaySlot

        def _lookup():
            return GatewaySlot.objects.filter(pk=gateway_slot).values_list(
                "provisioning_status", flat=True
            ).first()

        if connection.vendor == "postgresql" and org_id:
            from apps.tenants.context import tenant_context

            with transaction.atomic(), tenant_context(org_id):
                return _lookup()
        return _lookup()
    except Exception:
        logger.exception("provisioning status lookup failed for %s", gateway_slot)
        return None


def _on_message(client, userdata, message) -> None:
    # M3-02: every message now passes the pipeline's ordered gates; rejections
    # dead-letter with a reason. M5-04: telemetry from unapproved gateways is
    # held in the pending stream; rejected/suspended gateways are archived.
    global _messages_seen
    with _messages_lock:
        _messages_seen += 1  # M7: /metrics counter (best-effort — never blocks ingestion)
    topic = message.topic or ""
    # T1 topic shape: {org}/farms/{farm}/gateways/{slot}/... — the ACL pins
    # each gateway to publish under its OWN org's prefix, so segment 0 is the
    # trustworthy org for the RLS-scoped lookups below.
    org_from_topic = topic.split("/", 1)[0]
    handle_message(
        _redis(), topic, message.payload,
        db_fetch=_slot_db_fetch,
        status_fetch=lambda gw: _status_fetch(org_from_topic, gw),
    )


# ---------------------------------------------------------------------------
# M7 (v0.7): the :9091 /metrics surface (Observability §3.2) — subscriber_up
# drives the SubscriberTierDegraded page; delivery counters are evidence for
# the MET-AVAIL SLO math.
# ---------------------------------------------------------------------------
class _MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path != "/metrics":
            self.send_error(404)
            return
        body = (
            "# HELP subscriber_up Subscriber liveness (heartbeat per Observability §3.2)\n"
            "# TYPE subscriber_up gauge\n"
            "subscriber_up 1\n"
            "# HELP telemetry_messages_received_total MQTT messages handed to the pipeline\n"
            "# TYPE telemetry_messages_received_total counter\n"
            f"telemetry_messages_received_total {_messages_seen}\n"
            "# HELP subscriber_subscribed Both lanes granted by the broker (0 = ingestion DOWN)\n"
            "# TYPE subscriber_subscribed gauge\n"
            f"subscriber_subscribed {1 if _subscriptions_ok() else 0}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # scrape noise must not drown the broker log
        return None


def _serve_metrics() -> None:
    try:
        http.server.ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), _MetricsHandler).serve_forever()
    except OSError as exc:
        # Never take ingestion down for a scrape endpoint — but make it loud.
        logger.error("metrics server on :%s failed: %s", METRICS_PORT, exc)


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


def _handle_status(client, topic: str, payload: bytes) -> None:
    """Mirror retained gateway status into the pack liveness key."""
    import json

    from apps.alerts.services.redis_keys import pack_lwt_key
    from apps.devices.models import SaloonSensorSlot

    parts = topic.split("/")
    if len(parts) != 6 or parts[1] != "farms" or parts[3] != "gateways" or parts[5] != "status":
        return
    try:
        body = json.loads(payload)
    except (TypeError, ValueError):
        return
    status = body.get("status")
    if status not in {"online", "offline"}:
        return
    # The status topic's gateway segment is the sensor-slot identity in the
    # demo/simulator topology (slot_id doubles as the gateway id); the lwt
    # key lives per (saloon, slot) so the boss board can surface dead packs.
    slot = SaloonSensorSlot.objects.filter(slot_id=parts[4], org_id=parts[0]).first()
    if slot is None:
        return
    # Gateway-level status is retained; no TTL is used for durable LWT state.
    client.set(pack_lwt_key(slot.saloon_id, slot.slot_id), status)


def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    logger.warning("disconnected from broker: %s", reason_code)


def main() -> None:
    host = os.environ.get("MQTT_HOST", "127.0.0.1")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    client = build_client()
    threading.Thread(target=_serve_metrics, daemon=True, name="metrics").start()
    logger.info("booting subscriber → %s:%s", host, port)
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
