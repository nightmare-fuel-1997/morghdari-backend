"""Retroactive replay alert sweep (M4-08) — history without panic.

When a gateway's FIFO backlog finishes draining, every buffered window that
breached a cached threshold while nobody was watching gets a REPLAY_ANALYSIS
AlertInstance row with batch_id = {slot}:{first_device_ts} — idempotent on
re-run (an existing batch_id is skipped), and NEVER pushed: managers review
the past, nobody's phone buzzes for it (v0.4 TUTORIAL §3.7; Blueprint §5:
`batch_id` = `{slot}:{first_device_ts}`; skip already-covered windows).

Dispatch contract (Blueprint §5): the historical consumer decrements
replay:pending:{gateway_slot} per processed buffered entry; at ≤ 0 the
backlog is done and alerts.run_replay_alert_sweep dispatches exactly once via
the replay:sweeping:{gateway_slot} NX marker (concurrent workers race-safe;
the sweep deletes it in `finally` so the NEXT outage re-arms).
فارسی: بازپخشِ تاریخچه هرگز زنگ خطر نمی‌زند — فقط ردیفِ هشدارِ تاریخی می‌سازد.
"""

import logging

from django.db import transaction
from django.utils.dateparse import parse_datetime

from apps.alerts.models import AlertInstance
from apps.alerts.services.redis_keys import (
    replay_covered_key,
    replay_pending,
    replay_sweeping,
    thresholds_hash,
)

logger = logging.getLogger(__name__)


def system_redis():
    """Same client convention as sweeps (tests inject the integration client)."""
    import os

    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True)


def note_buffered_processed(client, gateway_slot: str | None, count: int) -> bool:
    """Count drained buffered entries down; at ≤ 0 dispatch the sweep ONCE.

    Returns True when this call is the one that dispatched. A negative
    counter (Redis restart lost the increments) is clamped to 0 — the backlog
    that JUST drained is by definition done.
    فارسی: شمارندهٔ بدهیِ بازپخش؛ رسیدن به صفر یعنی صف تمام شد و سویپ رانده می‌شود.
    """
    if count <= 0 or not gateway_slot:
        return False

    remaining = client.decrby(replay_pending(gateway_slot), count)
    if remaining > 0:
        return False
    if remaining < 0:
        client.set(replay_pending(gateway_slot), 0)

    # Blueprint §5 dispatch-once: concurrent drains race on this NX marker.
    if not client.set(replay_sweeping(gateway_slot), "1", nx=True):
        return False

    from apps.alerts.tasks import run_replay_alert_sweep

    run_replay_alert_sweep.delay(gateway_slot)
    return True

def _window_breaches(rows, thresholds: dict) -> dict:
    """Worst breaching value per metric inside a buffered window.

    Mirrors the realtime evaluation's comparison EXACTLY (evaluation.py:
    `{metric}_max` only — min-side cold detection is not a v0.4 rule), but
    without duration/velocity/grace semantics: retroactive analysis is
    historical review, not emergency paging.
    """
    worst: dict = {}
    for row in rows:
        metrics = row.metrics or {}
        for metric_key, values in metrics.items():
            if not isinstance(values, dict):
                continue
            max_str = thresholds.get(f"{metric_key}_max")
            if max_str is None:
                continue
            try:
                value = float(values.get("avg"))
            except (TypeError, ValueError):
                continue
            if value > float(max_str) and value > worst.get(metric_key, float("-inf")):
                worst[metric_key] = value
    return worst


def run_replay_sweep(gateway_slot: str | None = None) -> list:
    """Sweep drained buffered windows into REPLAY_ANALYSIS rows (never push).

    Per sensor slot: uncovered buffered rows (device_ts > replay:covered
    high-water mark) form ONE window with batch_id = {slot}:{first_device_ts};
    an existing batch_id skips the window (idempotent re-run / crash seam)
    while the pointer still advances. Returns created batch_ids.
    """
    from apps.farms.models import Saloon
    from apps.tenants.context import tenant_context
    from apps.telemetry.models import SaloonTelemetry

    client = system_redis()
    try:
        created = []
        buffered = SaloonTelemetry.objects.filter(is_buffered=True)
        for slot_id in buffered.values_list("slot_id", flat=True).distinct().iterator():
            rows_qs = buffered.filter(slot_id=slot_id).order_by("device_ts")
            covered = client.get(replay_covered_key(slot_id))
            if covered:
                rows_qs = rows_qs.filter(device_ts__gt=parse_datetime(covered))
            rows = list(rows_qs)
            if not rows:
                continue

            first_ts, last_ts = rows[0].device_ts, rows[-1].device_ts
            batch_id = f"{slot_id}:{first_ts.isoformat()}"
            saloon_id = rows[0].saloon_id
            org_id = str(rows[0].org_id)
            thresholds = client.hgetall(thresholds_hash(saloon_id))

            with transaction.atomic():
                with tenant_context(org_id):
                    already = AlertInstance.objects.filter(
                        source="REPLAY_ANALYSIS", batch_id=batch_id
                    ).exists()
                    if not already and thresholds:
                        saloon = Saloon.objects.only("farm_id").get(pk=saloon_id)
                        for metric, worst in _window_breaches(rows, thresholds).items():
                            AlertInstance.objects.create(
                                org_id=org_id,
                                farm_id=saloon.farm_id,
                                saloon_id=saloon_id,
                                metric_type=metric,
                                current_status="ACTIVE_TIER_1",
                                current_value=worst,
                                source="REPLAY_ANALYSIS",
                                batch_id=batch_id,
                            )
                            created.append(batch_id)
                            logger.warning(
                                "REPLAY_ANALYSIS alert (NEVER pushed): saloon=%s "
                                "metric=%s worst=%s batch=%s window=%s..%s",
                                saloon_id, metric, worst, batch_id, first_ts, last_ts,
                            )

            # Advance the high-water mark even when the batch already existed:
            # a crash between alert-write and pointer-write is exactly the
            # idempotency seam the batch_id re-check covers.
            client.set(replay_covered_key(slot_id), last_ts.isoformat())

        return created
    finally:
        if gateway_slot:
            # Re-arm: the NEXT outage's counter-zero must dispatch again.
            client.delete(replay_sweeping(gateway_slot))
