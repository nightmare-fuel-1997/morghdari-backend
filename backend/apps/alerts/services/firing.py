"""Alert firing — turns tracked breaches into AlertInstance DB rows (M4-04).

Coalescing (owner Q2): ONE active row per saloon+metric. A repeat spike while
an alert is already ACTIVE_TIER_* refreshes current_value and may re-promote
the tier, but never inserts a second row. The active Redis key
(`alert:active:saloon:{id}:{metric}`) tracks which alert is open so sweeps can
find and resolve/escalate it without querying Postgres first.
فارسی: هر سالن و سنجه فقط یک ردیفِ هشدار فعال دارد؛ تکرارِ مقدار فقط همان ردیف
را به‌روزرسانی می‌کند و ردیفِ جدیدی نمی‌سازد.
"""

import json
import logging
import uuid

from django.db import transaction

from apps.alerts.models import AlertInstance
from apps.alerts.services.redis_keys import active_alert_key
from apps.tenants.context import tenant_context

logger = logging.getLogger(__name__)


def _active_key(saloon_id: int, metric: str) -> str:
    return active_alert_key(saloon_id, metric)


def clear_active_blob(redis_client, saloon_id: int, metric: str) -> None:
    """Drop the open-alert pointer (BUG-9b full lifecycle). Called by
    force-resolve AND by the coalesce self-heal below — the sweep's resolve
    branch keeps its own delete (untouched).
    No TTL backstop by design: a TTL could expire mid-episode and a re-fire
    would insert a SECOND active row for the same saloon+metric, violating
    the one-active-row invariant the coalescer exists to protect.
    فارسی: بدون TTL — انقضای میانهٔ دوره ردیفِ تکراری میسازد؛ ترمیم در نقطهٔ خواندن."""
    redis_client.delete(active_alert_key(saloon_id, metric))


def fire_or_coalesce(
    redis_client,
    org_id: str,
    farm_id: int,
    saloon_id: int,
    metric: str,
    value: float,
    clock_now: float | None = None,
    source: str = "REALTIME",
    batch_id: str | None = None,
    reason: str = "",
) -> dict:
    """Create or coalesce an AlertInstance row inside tenant_context.

    Returns {"action": "fired"|"coalesced"|"dead-lettered", "alert_id": …}.
    فارسی: اگر هشدار فعال موجود باشد همان به‌روزرسانی می‌شود؛ وگرنه ردیفِ جدید ساخته می‌شود.
    """
    import redis as redis_lib

    from apps.farms.models import Saloon

    saloon_fk = Saloon.objects.only("id", "farm_id").get(pk=saloon_id)
    farm_fk_id = saloon_fk.farm_id

    active_key = _active_key(saloon_id, metric)
    existing_json = redis_client.get(active_key)

    with transaction.atomic():
        with tenant_context(org_id):
            if existing_json:
                # BUG-9b self-heal: the blob outlived its row (a closer that
                # forgot the clear, or a crash between the DB commit and the
                # Redis delete). A stale pointer must NEVER coalesce into a
                # dead row — drop it and fall through to the fresh-fire
                # branch so the metric pages again.
                # فارسی: اگر اشاره‌گر به ردیفِ بسته‌شده اشاره کند، پاکش کن و
                # ردیفِ تازه بساز؛ هرگز به ردیفِ مرده نچسب.
                existing = json.loads(existing_json)
                alert = AlertInstance.objects.get(id=existing["alert_id"])
                if alert.resolved_at is not None:
                    clear_active_blob(redis_client, saloon_id, metric)
                    existing_json = None
                    existing = None
            if existing_json:
                # Coalesce: refresh the existing open alert's value/tier.
                alert.current_value = value
                if existing.get("current_tier", 1) > alert.highest_tier_reached:
                    alert.highest_tier_reached = existing["current_tier"]
                alert.save(update_fields=["current_value", "highest_tier_reached"])
                action = "coalesced"
            else:
                alert = AlertInstance.objects.create(
                    org_id=org_id,
                    farm_id=farm_fk_id,
                    saloon_id=saloon_id,
                    metric_type=metric,
                    current_status="ACTIVE_TIER_1",
                    current_value=value,
                    source=source,
                    batch_id=batch_id,
                    reason=reason or None,
                )
                action = "fired"
                # M7-04 observability: created counter (Redis bridge — runbook §3.1).
                from apps.monitoring.metrics import bump_alert_created

                bump_alert_created(redis_client, "1", metric)

    # Stash the open-alert pointer AFTER the DB commit succeeds (D3 pattern).
    blob = json.dumps({
        "alert_id": str(alert.id),
        "org_id": str(org_id),  # callers may hold the UUID PK — keep dumps total
        "farm_id": farm_fk_id,
        "saloon_id": saloon_id,
        "metric": metric,
        "current_tier": 1,
        "reason": reason,
    })
    redis_client.set(active_key, blob)

    logger.info("alert %s: saloon=%s metric=%s value=%s", action, saloon_id, metric, value)
    return {"action": action, "alert_id": str(alert.id)}
