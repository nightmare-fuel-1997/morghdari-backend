"""v0.7 M7-04 — Prometheus metrics endpoint + Redis bridge (Observability §3.1).

فارسی: سنجه‌های پرومتئوس — نقطهٔ پایانی /api/v1/metrics/ و پلِ ردیس.

The endpoint must serve the Prometheus exposition format (text/plain), and
the Redis bridge must lift worker-side counters into the export so the
alert rules in monitoring/alerts.yml (TelemetryPipelineDown,
PendingApprovalBacklog, BeatHeartbeatStale) have real series to evaluate.
"""

import time

import pytest
from django.urls import reverse
from prometheus_client import generate_latest

from apps.monitoring.metrics import (
    ALERTS_CREATED,
    ALERTS_RESOLVED,
    BEAT_HEARTBEAT_TS,
    CELERY_QUEUE_DEPTH,
    DB_CONNECTIONS,
    REDIS_QUEUE_DEPTH,
    TELEMETRY_BUFFERED,
    TELEMETRY_INGESTED,
    TELEMETRY_PENDING_APPROVAL_DEPTH,
    WS_CONNECTIONS,
    bump_alert_created,
    bump_alert_resolved,
    bump_buffered,
    bump_ingested,
    note_beat_heartbeat,
    refresh_gauges,
)
from apps.monitoring.tasks import beat_heartbeat


def _redis():
    from apps.monitoring.metrics import _metrics_redis

    return _metrics_redis()


@pytest.fixture(autouse=True)
def _clean_metric_keys():
    client = _redis()
    try:
        for key in client.scan_iter("metrics:*"):
            client.delete(key)
    except Exception:  # noqa: BLE001 — Redis down: tests will fail loudly anyway
        pass
    yield


@pytest.mark.django_db(transaction=False)
def test_metrics_endpoint_serves_prometheus_format(client):
    resp = client.get(reverse("monitoring:metrics"))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")
    body = resp.content.decode()
    # Declared collectors must appear (runbook §3.1 names verbatim).
    for name in (
        "telemetry_ingested_total",
        "telemetry_buffered_total",
        "alerts_created_total",
        "alerts_resolved_total",
        "redis_queue_depth",
        "websocket_active_connections",
        "telemetry_pending_approval_depth",
        "celery_queue_depth",
        "celery_beat_heartbeat_timestamp_seconds",
    ):
        assert name in body, f"metric {name} missing from /metrics/ export"


@pytest.mark.django_db(transaction=False)
def test_redis_bridge_lifts_worker_counters_into_export(client):
    r = _redis()
    bump_ingested(r, "org-1", "7", 3)
    bump_ingested(r, "org-1", "8", 2)
    bump_buffered(r, "org-1", 4)
    bump_alert_created(r, "1", "nh3")
    bump_alert_resolved(r, "2")

    refresh_gauges()
    body = generate_latest().decode()

    assert 'telemetry_ingested_total{org_id="org-1",saloon_id="7"} 3' in body
    assert 'telemetry_ingested_total{org_id="org-1",saloon_id="8"} 2' in body
    assert 'telemetry_buffered_total{org_id="org-1"} 4' in body
    # prometheus_client renders labels in ALPHABETICAL order.
    assert 'alerts_created_total{metric_type="nh3",tier="1"} 1' in body
    assert 'alerts_resolved_total{tier="2"} 1' in body


@pytest.mark.django_db(transaction=False)
def test_beat_heartbeat_task_and_gauge(client):
    ts = beat_heartbeat()
    assert abs(ts - time.time()) < 5

    refresh_gauges()
    body = generate_latest().decode()
    assert "celery_beat_heartbeat_timestamp_seconds " in body


@pytest.mark.django_db(transaction=False)
def test_queue_depth_gauges_reflect_redis_streams(client):
    r = _redis()
    # Streams persist across tests — start from a clean key so XLEN is ours.
    r.delete("telemetry:pending_approval")
    r.xadd("telemetry:pending_approval", {"k": "v"})
    r.xadd("telemetry:pending_approval", {"k": "v"})

    refresh_gauges()
    body = generate_latest().decode()

    assert "telemetry_pending_approval_depth 2" in body
    assert 'redis_queue_depth{queue="pending_approval"} 2' in body


@pytest.mark.django_db(transaction=False)
def test_bridge_bumps_are_silent_when_redis_down():
    """A metrics bump must never break the hot path — Redis down ⇒ no raise."""

    class _DeadRedis:
        def __getattr__(self, _):
            def boom(*_a, **_k):
                raise RuntimeError("redis is down")

            return boom

    dead = _DeadRedis()
    bump_ingested(dead, "org-1", "7")
    bump_buffered(dead, "org-1")
    bump_alert_created(dead, "1", "nh3")
    bump_alert_resolved(dead, "1")
    note_beat_heartbeat(dead)
    refresh_gauges()  # must also swallow its own failures