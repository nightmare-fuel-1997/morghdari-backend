"""Prometheus metrics — Observability & Monitoring Runbook §3.1 (v0.7 M7-04).

فارسی: سنجه‌های پرومتئوسِ جنگو؛ پلِ ردیس میان کارگرِ سلیتری و درخواستِ
اسکرپ (چون هرکدام پروسهٔ جدا هستند).

Multi-process reality: the Celery worker ingests telemetry and the daphne
process serves /api/v1/metrics/. A plain in-process prometheus_client
counter would live in the worker and NEVER appear in daphne's export, so
every producer-side bump writes to a Redis counter/ring and the scrape-time
refresh_gauges() reads Redis back into the collectors before export. The
bridged counters are monotonic (INCRBY only), so Prometheus treats them as
proper counters.

Metric names and labels are VERBATIM from the runbook §3.1 so the alert
rules in monitoring/alerts.yml (TelemetryPipelineDown, PendingApprovalBacklog,
BeatHeartbeatStale) and any dashboard queries match the spec exactly.

Honest-null rule (D22/D23): gauges whose producer does not exist yet are
declared but never set — an absent series is "not measured here", never a
fabricated zero. INSERT_BUFFER_LENGTH is exactly that today.
"""

import os
import time

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Declared collectors (runbook §3.1, names/labels verbatim)
# ---------------------------------------------------------------------------
TELEMETRY_INGESTED = Counter(
    "telemetry_ingested_total",
    "Total telemetry messages ingested",
    ["org_id", "saloon_id"],
)
TELEMETRY_BUFFERED = Counter("telemetry_buffered_total", "Buffered (replay) messages", ["org_id"])
ALERTS_CREATED = Counter("alerts_created_total", "Alerts created", ["tier", "metric_type"])
ALERTS_RESOLVED = Counter("alerts_resolved_total", "Alerts resolved", ["tier"])
INGESTION_LATENCY = Histogram(
    "telemetry_ingestion_latency_seconds",
    "End-to-end ingestion latency",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2],
)
DB_CONNECTIONS = Gauge("db_connections_active", "Active PostgreSQL connections")
REDIS_QUEUE_DEPTH = Gauge("redis_queue_depth", "Telemetry buffer queue depth", ["queue"])
WS_CONNECTIONS = Gauge("websocket_active_connections", "Active WebSocket connections")
ALERT_EVAL_LATENCY = Histogram(
    "alert_eval_latency_seconds",
    "Inline threshold evaluation latency",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)
TELEMETRY_PENDING_APPROVAL_DEPTH = Gauge(
    "telemetry_pending_approval_depth", "XLEN of telemetry:pending_approval stream"
)
INSERT_BUFFER_LENGTH = Gauge(
    "insert_buffer_length", "Rows accumulated in the insert buffer awaiting batch flush"
)
# Runbook §3.3 — Celery queue depth (broker is Redis, so LLEN is the truth).
CELERY_QUEUE_DEPTH = Gauge("celery_queue_depth", "Celery broker queue depth", ["queue"])
# Beat heartbeat — the BeatHeartbeatStale rule (alerts.yml) reads this.
BEAT_HEARTBEAT_TS = Gauge("celery_beat_heartbeat_timestamp_seconds", "Last beat heartbeat (unix ts)")

# ---------------------------------------------------------------------------
# Redis bridge keys (producers bump; refresh_gauges() drains at scrape time)
# ---------------------------------------------------------------------------
K_INGESTED = "metrics:ingested:{org}:{saloon}"
K_BUFFERED = "metrics:buffered:{org}"
K_ALERTS_CREATED = "metrics:alerts_created:{tier}:{metric_type}"
K_ALERTS_RESOLVED = "metrics:alerts_resolved:{tier}"
K_INGEST_LATENCY_RING = "metrics:ingest_latency_ring"
K_EVAL_LATENCY_RING = "metrics:alert_eval_latency_ring"
K_BEAT_TS = "metrics:beat_heartbeat_ts"
_LATENCY_RING_MAX = 2000  # bounded: latencies are advisory, never alert-critical

_REDIS_URL = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")


def _metrics_redis():
    import redis

    return redis.Redis.from_url(_REDIS_URL, decode_responses=True, socket_timeout=0.6)


def _safe(fn, *args, **kwargs):
    """Auxiliary metrics must NEVER break the hot path — silent-fail bumps."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — metrics are advisory by design
        return None


# ---------------------------------------------------------------------------
# Producer-side bumps (Celery worker / alert services / beat)
# ---------------------------------------------------------------------------
def bump_ingested(client, org_id, saloon_id, count=1):
    _safe(client.incrby, K_INGESTED.format(org=org_id, saloon=saloon_id), count)


def bump_buffered(client, org_id, count=1):
    _safe(client.incrby, K_BUFFERED.format(org=org_id), count)


def bump_alert_created(client, tier, metric_type, count=1):
    _safe(client.incrby, K_ALERTS_CREATED.format(tier=tier, metric_type=metric_type), count)


def bump_alert_resolved(client, tier, count=1):
    _safe(client.incrby, K_ALERTS_RESOLVED.format(tier=tier), count)


def note_ingestion_latency(client, seconds):
    _safe(client.rpush, K_INGEST_LATENCY_RING, repr(seconds))
    _safe(client.ltrim, K_INGEST_LATENCY_RING, -_LATENCY_RING_MAX, -1)


def note_alert_eval_latency(client, seconds):
    _safe(client.rpush, K_EVAL_LATENCY_RING, repr(seconds))
    _safe(client.ltrim, K_EVAL_LATENCY_RING, -_LATENCY_RING_MAX, -1)


def note_beat_heartbeat(client=None):
    """Beat heartbeat: write the unix timestamp to Redis (daphne exports it)."""
    if client is None:
        client = _metrics_redis()
    _safe(client.set, K_BEAT_TS, repr(time.time()))


# ---------------------------------------------------------------------------
# Scrape-time refresh (daphne, /api/v1/metrics/)
# ---------------------------------------------------------------------------
def _refresh_counters(client):
    for key in client.scan_iter(f"{K_INGESTED.split('{')[0]}*"):
        try:
            _, _, rest = key.split(":", 2)
            org_id, saloon_id = rest.rsplit(":", 1)
        except ValueError:
            continue
        val = int(client.get(key) or 0)
        TELEMETRY_INGESTED.labels(org_id=org_id, saloon_id=saloon_id)._value.set(val)
    for key in client.scan_iter(f"{K_BUFFERED.split('{')[0]}*"):
        try:
            _, _, org_id = key.split(":", 2)
        except ValueError:
            continue
        TELEMETRY_BUFFERED.labels(org_id=org_id)._value.set(int(client.get(key) or 0))
    for key in client.scan_iter(f"{K_ALERTS_CREATED.split('{')[0]}*"):
        try:
            _, _, tier, metric_type = key.split(":", 3)
        except ValueError:
            continue
        ALERTS_CREATED.labels(tier=tier, metric_type=metric_type)._value.set(int(client.get(key) or 0))
    for key in client.scan_iter(f"{K_ALERTS_RESOLVED.split('{')[0]}*"):
        try:
            _, _, tier = key.split(":", 2)
        except ValueError:
            continue
        ALERTS_RESOLVED.labels(tier=tier)._value.set(int(client.get(key) or 0))


def _refresh_rings(client):
    for ring, histogram in ((K_INGEST_LATENCY_RING, INGESTION_LATENCY),
                            (K_EVAL_LATENCY_RING, ALERT_EVAL_LATENCY)):
        try:
            while True:
                item = client.lpop(ring)
                if item is None:
                    break
                histogram.observe(float(item))
        except Exception:  # noqa: BLE001
            pass


def _refresh_gauges(client):
    from django.db import connection

    if connection.vendor == "postgresql":
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
                DB_CONNECTIONS.set(int(cursor.fetchone()[0]))
        except Exception:  # noqa: BLE001
            pass

    streams = {
        "realtime": "telemetry:realtime",
        "historical": "telemetry:historical",
        "pending_approval": "telemetry:pending_approval",
    }
    for label, stream in streams.items():
        try:
            depth = client.xlen(stream)
        except Exception:  # noqa: BLE001
            continue
        REDIS_QUEUE_DEPTH.labels(queue=label).set(depth)
        if label == "pending_approval":
            TELEMETRY_PENDING_APPROVAL_DEPTH.set(depth)

    # WS liveness sets (ws.py): ws:active:{user} SCARD sums — bounded by user count.
    try:
        total = 0
        for key in client.scan_iter("ws:active:*"):
            total += client.scard(key)
        WS_CONNECTIONS.set(total)
    except Exception:  # noqa: BLE001
        pass

    # Celery broker queues (Redis broker — LLEN is the depth).
    for queue in ("celery", "telemetry_realtime", "telemetry_historical", "alerts", "audit", "provisioning"):
        try:
            CELERY_QUEUE_DEPTH.labels(queue=queue).set(client.llen(queue))
        except Exception:  # noqa: BLE001
            pass

    try:
        raw = client.get(K_BEAT_TS)
        if raw is not None:
            BEAT_HEARTBEAT_TS.set(float(raw))
    except Exception:  # noqa: BLE001
        pass


def refresh_gauges():
    """Pull Redis-bridged values into the collectors. Never raises — a scrape
    must stay up even when Redis is down (export what we already have)."""
    try:
        client = _metrics_redis()
        _refresh_counters(client)
        _refresh_rings(client)
        _refresh_gauges(client)
    except Exception:  # noqa: BLE001
        pass