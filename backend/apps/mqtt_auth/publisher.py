"""Outbound MQTT publisher — the backend's first publishing capability (E1).

WHY this file did not exist until now: `paho-mqtt` has been a declared
dependency since v0.1, but the only importer was the INBOUND subscriber
(`morghdari/subscriber/main.py`). Topics T3–T9, `add_dev`, and the whole
`django-backend` broker principal were contract-only — specified in
`versions/v0.5-provisioning-zero-touch/04-step-by-step-implementation-tasks.md`
tasks E1–E6 and never built. This module is v0.5 task E1, at the exact path the
plan named.

Design constraints that come from the rest of the system:

* **Never raise.** Publishing is a side effect of a business decision that has
  already been committed to Postgres. A broker outage must degrade to "not
  published yet", never to a 500 on an installer's submit. Callers record the
  failure on the row (`gateway_commands.last_error`, `gateway_slots.
  last_publish_error`) and the convergence sweep retries.
* **QoS 1 + retained is the delivery model** (AD-3): the desired-state document
  is retained, so an offline device receives it on reconnect without any queue
  bookkeeping on our side.
* **One process-local client.** Celery prefork workers each hold one connection
  and re-create it after a fork or a dead socket (`_client` checks
  `is_connected()`). No shared global across processes — paho clients are not
  fork-safe.
* **Injectable.** `set_client()` lets tests substitute a fake with a
  `publish()` that records calls, so no test needs a broker.
"""

from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

QOS_AT_LEAST_ONCE = 1
DEFAULT_TIMEOUT_SEC = 5.0

_client_lock = threading.Lock()
_client = None
_client_config = None
# True while a caller-installed (test) client is in place. Such a client is
# returned as-is: it has no is_connected()/loop_stop(), and the health check
# below would otherwise tear it down and fall through to "not configured".
_client_injected = False


def _config() -> dict:
    """Broker settings from the environment, read once per client build."""
    return {
        "host": os.environ.get("MQTT_HOST", "").strip(),
        "port": int(os.environ.get("MQTT_PORT", "1883")),
        "tls": os.environ.get("MQTT_TLS", "").strip().lower() in {"1", "true", "yes"},
        "tls_ca": os.environ.get("MQTT_TLS_CA", "").strip() or None,
        "tls_insecure": os.environ.get("MQTT_TLS_INSECURE", "").strip().lower()
        in {"1", "true", "yes"},
        # The principal documented in MQTT Topic Map §3.3. It is high-privilege
        # (it may publish to any org's topics — accepted risk S3), so it lives
        # in the vault like every other secret.
        "username": os.environ.get("PUBLISHER_USERNAME", "").strip() or None,
        "password": os.environ.get("PUBLISHER_PASSWORD", ""),
        "client_id": os.environ.get("PUBLISHER_CLIENT_ID", "django-backend"),
    }


def _build_client():
    """Connect a fresh paho client. Returns None when publishing is not configured."""
    import paho.mqtt.client as mqtt

    cfg = _config()
    if not cfg["host"]:
        # Deliberate: a dev laptop or a unit-test run has no broker, and that
        # must read as "not configured", not as an error.
        logger.debug("MQTT_HOST unset — publisher disabled")
        return None

    # MQTT 3.1.1 is the fleet standard (owner decision 2026-10-18, Tracker §4t).
    # Pinned explicitly so the choice is grep-able, never a library default.
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=cfg["client_id"],
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    if cfg["username"]:
        client.username_pw_set(cfg["username"], cfg["password"])

    if cfg["tls"]:
        import ssl

        context = ssl.create_default_context(cafile=cfg["tls_ca"])
        if cfg["tls_insecure"]:
            # Same accepted risk as the subscriber's internal leg: the docker
            # hostname is in no public SAN. Scope is the publisher→broker hop
            # inside the bridge network only.
            logger.warning("MQTT_TLS_INSECURE=1 — hostname verification OFF (staging only!)")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        client.tls_set_context(context)

    try:
        client.connect(cfg["host"], cfg["port"], keepalive=60)
    except OSError as exc:
        logger.warning("publisher cannot reach %s:%s (%s)", cfg["host"], cfg["port"], exc)
        return None
    client.loop_start()  # background thread: publishes are not blocked on reads
    return client


def get_client():
    """Process-local client, rebuilt when absent or disconnected."""
    global _client, _client_config
    with _client_lock:
        if _client_injected:
            return _client
        cfg = _config()
        if _client is not None and _client_config == cfg:
            try:
                if _client.is_connected():
                    return _client
            except Exception:  # pragma: no cover - defensive
                pass
            _shutdown(_client)
            _client = None
        _client = _build_client()
        _client_config = cfg
        return _client


def _shutdown(client) -> None:
    try:
        client.loop_stop()
        client.disconnect()
    except Exception:  # pragma: no cover - defensive
        pass


def set_client(client) -> None:
    """Test hook: install a fake client (or None to force 'not configured')."""
    global _client, _client_config, _client_injected
    with _client_lock:
        if _client is not None and _client is not client and not _client_injected:
            _shutdown(_client)
        _client = client
        _client_injected = client is not None
        _client_config = _config() if client is not None else None


def reset() -> None:
    """Drop the cached client — used by tests and after a fork."""
    set_client(None)


def publish(topic: str, payload, *, retain: bool = False,
            qos: int = QOS_AT_LEAST_ONCE, timeout: float = DEFAULT_TIMEOUT_SEC) -> bool:
    """Publish one message. Returns True only when the broker acknowledged it.

    `payload` may be a dict/list (JSON-encoded here) or a str/bytes already
    serialized by the caller.
    """
    client = get_client()
    if client is None:
        return False

    if isinstance(payload, (dict, list)):
        body = json.dumps(payload, separators=(",", ":"), default=str)
    elif isinstance(payload, bytes):
        body = payload
    else:
        body = str(payload)

    try:
        info = client.publish(topic, body, qos=qos, retain=retain)
    except Exception:
        logger.exception("publish raised for topic=%s", topic)
        return False

    if getattr(info, "rc", None) not in (None, 0):
        logger.warning("publish rejected locally for topic=%s (rc=%s)", topic, info.rc)
        return False

    try:
        # Bounded wait: a broker that accepted the socket but never PUBACKs must
        # not hold a Celery worker open indefinitely.
        info.wait_for_publish(timeout=timeout)
    except (ValueError, RuntimeError) as exc:
        logger.warning("publish not acknowledged for topic=%s (%s)", topic, exc)
        return False
    except Exception:
        logger.exception("publish wait raised for topic=%s", topic)
        return False

    try:
        return bool(info.is_published())
    except Exception:  # pragma: no cover - fakes may not implement it
        return True
