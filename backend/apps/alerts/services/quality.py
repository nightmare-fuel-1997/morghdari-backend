"""MET-ALERT-QUALITY export — numbers, not vibes (Observability §5.2).

Implemented as exportable functions (Tracker §4l-Q8) so the v0.4 retrospective
and (later, M7) Grafana render consume the SAME definitions. Pure ORM
computations on resolved alerts — no orchestration, no side effects:
- alert_mttr_seconds{tier}: mean(resolved_at − triggered_at) per tier
  (tier = highest_tier_reached, the alert's peak severity)
- alert_ack_to_resolve_seconds: mean resolution time for alerts that
  received ≥1 acknowledgement vs those that did not (Observability §5.2
  says 'vs not' — the comparison pair is the export)
- alerts_per_flock: alert count per flock cycle (REALTIME only — REPLAY_ANALYSIS
  is historical review, not operational load) plus that flock's mortality %
  so the alerts-vs-mortality correlation surface exists for the retrospective.

فارسی: خروجیِ کیفیت هشدار — میانگین زمانِ رفع به تفکیک سطح، اثرِ تأییدِ دستی روی
مدتِ رفع، و نسبتِ هشدار به هر دورهٔ پرورش.
"""

import logging

from django.db.models import (
    Avg,
    BooleanField,
    Case,
    F,
    Value,
    When,
)
# Django 5.0 removed ExtractEpoch. Backend-agnostic replacement: fetch the
# timestamp pairs and average the duration in Python (seconds) - exact on both
# the Postgres prod path and the SQLite test fallback (no native duration
# extraction on sqlite).


from apps.alerts.models import (
    AlertAcknowledgement,
    AlertInstance,
    MortalityRecord,
)
from apps.telemetry.models import FlockCycle

logger = logging.getLogger(__name__)


def mttr_seconds_by_tier() -> dict:
    """mean(resolved_at − triggered_at) per tier (highest_tier_reached), in
    whole seconds. Only resolved, non-replay alerts answer the question."""
    rows = (
        AlertInstance.objects
        .filter(resolved_at__isnull=False)
        .exclude(source="REPLAY_ANALYSIS")
        .values_list("highest_tier_reached", "resolved_at", "triggered_at")
    )
    sums, counts = {}, {}
    for tier, resolved_at, triggered_at in rows:
        if resolved_at is None or triggered_at is None:
            continue
        sec = (resolved_at - triggered_at).total_seconds()
        sums[tier] = sums.get(tier, 0.0) + sec
        counts[tier] = counts.get(tier, 0) + 1
    return {tier: sums[tier] / counts[tier] for tier in sums}


def _resolution_dict() -> dict:
    """Slice resolved alerts by 'acked' vs 'never-acked' (uses the ack table,
    not the pause key — row truth)."""
    acked_ids = (
        AlertAcknowledgement.objects
        .values_list("alert_id", flat=True)
        .distinct()
    )
    qs = (
        AlertInstance.objects
        .filter(resolved_at__isnull=False)
        .exclude(source="REPLAY_ANALYSIS")
        .annotate(
            acked=Case(
                When(id__in=acked_ids, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
    )
    return {
        "acked_mean_seconds": _mean_resolution_sec(qs.filter(acked=True)),
        "never_acked_mean_seconds": _mean_resolution_sec(qs.filter(acked=False)),
    }


def _mean_resolution_sec(qs) -> float:
    pairs = qs.values_list("resolved_at", "triggered_at")
    secs = [(r - t).total_seconds() for r, t in pairs if r is not None and t is not None]
    return sum(secs) / len(secs) if secs else 0.0


def ack_to_resolve_seconds() -> dict:
    """The §5.2 comparison: mean(a→r) for acknowledged vs never-acknowledged
    alerts. Returns {'acked_mean_seconds', 'never_acked_mean_seconds'}."""
    return _resolution_dict()


def alerts_per_flock(close_only: bool = True) -> list:
    """alert count + mortality % per flock cycle (REALTIME alerts only).
    close_only=False includes open flocks (running cycle snapshot)."""
    from apps.alerts.models import AlertInstance, MortalityRecord
    from apps.telemetry.models import FlockCycle

    flocks = FlockCycle.objects.all()
    if close_only:
        flocks = flocks.filter(end_date__isnull=False)

    out = []
    for flock in flocks.iterator():
        counted = (
            AlertInstance.objects
            .filter(saloon_id=flock.saloon_id, source="REALTIME")
            .count()
        )
        total_mortality = sum(
            MortalityRecord.objects.filter(flock_id=flock.flock_id)
            .values_list("count", flat=True)
        )
        mortality_pct = 0.0
        if flock.placement_count:
            mortality_pct = round(total_mortality / flock.placement_count * 100.0, 2)
        out.append({
            "flock_id": flock.flock_id,
            "saloon_id": flock.saloon_id,
            "alerts": counted,
            "mortality_pct": mortality_pct,
        })
    return out


def snapshot() -> dict:
    """One call for the CHANGELOG/[v0.4] retrospective record."""
    return {
        "mttr_seconds_by_tier": mttr_seconds_by_tier(),
        "ack_to_resolve_seconds": ack_to_resolve_seconds(),
        "alerts_per_flock": alerts_per_flock(),
    }