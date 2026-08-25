"""Stream-draining consumers â€” XACK strictly AFTER the DB commit (M3-05/06).

Idempotency model (why exactly-one row survives crashes):
- subscriber stamps every entry with recv_ts and sets the per-slot dedup key;
- the worker uses THAT recv_ts as server_received_ts â€” deterministic per
  stream entry â€” so UNIQUE(server_received_ts, slot_id, org_id) +
  bulk_create(ignore_conflicts=True) absorbs any redelivery of the same
  entry (crash before OR after commit);
- only then XACK. At-least-once + idempotency = effectively-once.

Org grouping: FORCE RLS WITH CHECK rejects a mixed-org batch under one GUC,
so rows are grouped per org and each group commits inside its own
transaction.atomic() + tenant_context(org).

Historical lane pacing (M3-06): rates come from system_configuration EVERY
cycle (D21 config-is-data) â€” a global inter-batch sleep derived from
replay_rate_limit_per_min plus a sliding per-org window at
replay_rate_limit_per_org_per_min, so one tenant's replay storm can never
starve live traffic (noisy-neighbour fairness).
ÙØ§Ø±Ø³ÛŒ: Ù…ØµØ±Ùâ€ŒÚ©Ù†Ù†Ø¯Ù‡Ù” Ø§Ø³ØªØ±ÛŒÙ…Ø› Ø§Ù…Ø¶Ø§ ÙÙ‚Ø· Ø¨Ø¹Ø¯ Ø§Ø² Ú©Ø§Ù…ÛŒØªØ› Ø±Ø¯ÛŒÙâ€ŒÙ‡Ø§ Ø¨Ù‡ ØªÙÚ©ÛŒÚ© Ø³Ø§Ø²Ù…Ø§Ù† Ø¯Ø§Ø®Ù„
ØªØ±Ø§Ú©Ù†Ø´ Ø¨Ø§ tenant_contextØ› Ø³Ø±Ø¹ØªÙ Ù…Ø³ÛŒØ± ØªØ§Ø±ÛŒØ®ÛŒ Ù‡Ø± Ú†Ø±Ø®Ù‡ Ø§Ø² ØªÙ†Ø¸ÛŒÙ…Ø§Øª Ø®ÙˆØ§Ù†Ø¯Ù‡ Ù…ÛŒâ€ŒØ´ÙˆØ¯.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone as dt_timezone
from functools import lru_cache

import redis
from celery import shared_task
from django.db import transaction
from django.utils.dateparse import parse_datetime

from apps.system_config.models import SystemConfiguration
from apps.telemetry.models import SaloonTelemetry
from morghdari.subscriber.pipeline import (
    STREAM_HISTORICAL,
    STREAM_REALTIME,
    trim_streams,
)

logger = logging.getLogger(__name__)

CONSUMER_GROUP = "telemetry_workers"   # Q2: contract name (Tracker Â§4k)
BATCH_SIZE_DEFAULT = 50

_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
        _redis_client = redis.Redis.from_url(url, decode_responses=True)
    return _redis_client


def _consumer_name() -> str:
    return os.environ.get("SUBSCRIBER_CONSUMER_ID", "celery-worker")


def _ensure_group(client, stream: str) -> None:
    try:
        client.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _config_int(key: str, default: int) -> int:
    raw = SystemConfiguration.objects.filter(key=key).values_list("value", flat=True).first()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _to_model_kwargs(entry_fields: dict) -> dict:
    """Stream entry â†’ SaloonTelemetry kwargs. Deterministic timestamps make
    redeliveries conflict-free under the hypertable's UNIQUE constraint."""
    received = float(entry_fields["recv_ts"])
    return {
        "server_received_ts": datetime.fromtimestamp(received, tz=dt_timezone.utc),
        "device_ts": parse_datetime(entry_fields["device_ts"]),
        "org_id": entry_fields["org_id"],
        "slot_id": entry_fields["slot_id"],
        "is_buffered": entry_fields["is_buffered"] == "True",
        "metrics": json.loads(entry_fields.get("metrics", "{}")),
        "clock_drift": entry_fields.get("clock_drift") == "True",
    }


class HistoricalPacer:
    """Sliding-window per-org throttle + global inter-batch sleep.

    `sleeper` is injectable so tests never actually wait — production uses
    time.sleep. The clock is injected for the same reason (D21 pacing math
    must be unit-testable without real minutes passing).
    فارسی: خواب و ساعت تزریقی‌اند تا تست‌ها واقعاً منتظر نمانند.
    """

    def __init__(self, config_getter, clock=time.monotonic, sleeper=time.sleep):
        self._config = config_getter
        self._clock = clock
        self._sleep = sleeper
        self._seen: dict[str, list[float]] = {}

    def sleep_seconds(self, org: str | None) -> float:
        global_rate = max(self._config("replay_rate_limit_per_min", 50), 1)
        return 60.0 / global_rate

    def wait_for_org(self, org: str) -> None:
        org_rate = max(self._config("replay_rate_limit_per_org_per_min", 20), 1)
        window = 60.0
        now = self._clock()
        stamps = [t for t in self._seen.get(org, []) if now - t < window]
        if len(stamps) >= org_rate:
            delay = window - (now - stamps[0]) + 0.01
            if delay > 0:
                self._sleep(delay)
            now = self._clock()
            stamps = [t for t in stamps if now - t < window]
        stamps.append(self._clock())
        self._seen[org] = stamps


def resolve_saloon(fields: dict) -> str:
    from apps.devices.models import SaloonSensorSlot

    slot = SaloonSensorSlot.objects.only("saloon_id").get(pk=fields["slot_id"])
    return slot.saloon_id


def drain_stream(stream: str, count: int = BATCH_SIZE_DEFAULT, pacer=None) -> int:
    """One consumer cycle: read NEW entries PLUS this consumer's own pending
    ones (crash recovery between read and ack), insert per-org batches,
    COMMIT, THEN ack. Returns entries processed."""
    client = _redis()
    _ensure_group(client, stream)
    consumer = _consumer_name()

    batches = list(
        client.xreadgroup(CONSUMER_GROUP, consumer, {stream: ">"}, count=count, block=1000)
    )
    # Crash recovery: OUR OWN pending deliveries (id "0" = the PEL).
    batches.extend(client.xreadgroup(CONSUMER_GROUP, consumer, {stream: "0"}, count=count))
    if not batches:
        return 0

    processed = 0
    acked: set[str] = set()
    for _stream, entries in batches:
        if not entries:
            continue
        by_org: dict[str, list[dict]] = {}
        ids: list[str] = []
        for entry_id, fields in entries:
            ids.append(entry_id)
            kwargs = _to_model_kwargs(fields)
            kwargs["saloon_id"] = resolve_saloon(fields)
            by_org.setdefault(kwargs["org_id"], []).append(kwargs)

        for org, rows in by_org.items():
            from apps.tenants.context import tenant_context

            # FORCE RLS: one GUC per commit; mixed-org batches are impossible.
            with transaction.atomic():
                with tenant_context(org):
                    SaloonTelemetry.objects.bulk_create(
                        (SaloonTelemetry(**row) for row in rows),
                        ignore_conflicts=True,  # same-entry redelivery ⇒ no-op
                    )
            processed += len(rows)
            if pacer is not None:
                time.sleep(pacer.sleep_seconds(org))
                pacer.wait_for_org(org)

        # THE rule: acknowledge only after the DB commit above succeeded.
        # Same entry may appear in BOTH the '>' read and the PEL read within
        # one cycle — ack counts ENTRIES, so the return value stays honest.
        if ids:
            client.xack(stream, CONSUMER_GROUP, *ids)
            acked.update(ids)
            if pacer is not None:
                time.sleep(pacer.sleep_seconds(org))
                pacer.wait_for_org(org)

        # THE rule: acknowledge only after the DB commit above succeeded.
        if ids:
            client.xack(stream, CONSUMER_GROUP, *ids)
            acked.update(ids)

    trim_streams(client)  # M3-07: bounded streams on every touch
    return len(acked)  # unique entries acknowledged; row count may be lower


@shared_task(name="telemetry.process_realtime_payload")
def process_realtime_payload(count: int = BATCH_SIZE_DEFAULT) -> int:
    """Latency-sensitive lane: no rate limit (TUTORIAL Â§3.5).

    Beat re-kicks this task every 10s (CELERY_BEAT_SCHEDULE) â€” one cycle per
    kick keeps workers stateless and crash-simple; a dead chain self-heals on
    the next tick. No self-rescheduling recursion to reason about.
    """
    return drain_stream(STREAM_REALTIME, count)


@shared_task(name="telemetry.process_historical_payload")
def process_historical_payload(count: int = BATCH_SIZE_DEFAULT) -> int:
    """Polite-under-storms lane: config-capped pacing, no redeploy needed."""
    pacer = HistoricalPacer(lambda key, default: _config_int(key, default))
    return drain_stream(STREAM_HISTORICAL, count, pacer=pacer)


