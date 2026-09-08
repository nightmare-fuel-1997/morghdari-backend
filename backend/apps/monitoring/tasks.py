"""Monitoring tasks (v0.7 M7-04) — autodiscovered by config.celery.

فارسی: کارهای نظارتی؛ ضربانِ قلبِ بیت برای هشدار BeatHeartbeatStale.

The BeatHeartbeatStale alert rule (monitoring/alerts.yml) reads
`celery_beat_heartbeat_timestamp_seconds` — a gauge exported by DAPHNE's
/metrics/ endpoint. Beat and daphne are separate processes, so this task
writes the unix timestamp to Redis and the scrape-time refresh
(monitoring.metrics.refresh_gauges) lifts it into the gauge. A dead beat
means the timestamp goes stale and the rule fires after 300 s.
"""

import time

from celery import shared_task

from apps.monitoring.metrics import note_beat_heartbeat


@shared_task(name="monitoring.beat_heartbeat")
def beat_heartbeat() -> float:
    """Record the beat heartbeat timestamp (Redis bridge → daphne gauge)."""
    note_beat_heartbeat()
    return time.time()