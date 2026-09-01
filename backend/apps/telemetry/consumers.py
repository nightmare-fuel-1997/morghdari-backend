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

CONSUMERS_REDIS_URL = os.environ.get(
    "REDIS_URL", "redis://:dev_redis_password@localhost:6379"
)  # captured ONCE at import — test modules may point this elsewhere via patching

_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(CONSUMERS_REDIS_URL, decode_responses=True)
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


def _config_snapshot() -> dict:
    """All drain-cycle knobs in ONE query instead of one query per key per org
    batch; D21's per-cycle freshness is preserved (still read every cycle)."""
    return dict(
        SystemConfiguration.objects.filter(
            key__in=("sensor_status_ttl_sec", "replay_rate_limit_per_min", "replay_rate_limit_per_org_per_min")
        ).values_list("key", "value")
    )


def _cfg_getter(snapshot: dict):
    def getter(key: str, default: int) -> int:
        try:
            return int(snapshot.get(key, default))
        except (TypeError, ValueError):
            return default

    return getter


def _batched_saloon_map(slot_ids: list) -> dict:
    """slot pk → saloon id in ONE query (replaces the per-entry GET N+1).
    Unknown slots stay absent → DLQ, same semantics as resolve_saloon."""
    from apps.devices.models import SaloonSensorSlot

    return dict(
        SaloonSensorSlot.objects.filter(pk__in=set(slot_ids)).values_list("pk", "saloon_id")
    )


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


def resolve_saloon(fields: dict):
    """Slot pk → saloon id. Unknown/deleted slots dead-letter (never crash).

    Pipeline guarantees resolution pre-publish, but stale entries can outlive
    their slot rows (demo resets, provisioning deletions) — those are poison
    and belong in the DLQ with a reason, exactly like the subscriber treats
    UNKNOWN_SLOT (Q1 family).
    فارسی: اسلاتِ ناشناخته در زمانِ درین = پیامِ سالم به صندوق مرده، نه کرش.
    """
    from apps.devices.models import SaloonSensorSlot

    try:
        slot = SaloonSensorSlot.objects.only("saloon_id").get(pk=fields["slot_id"])
    except SaloonSensorSlot.DoesNotExist:
        return None
    return slot.saloon_id


def touch_sensor_status(client, pairs, config_getter=_config_int) -> None:
    """M4-07 pipeline step 5 (Walkthrough Part 3): every LIVE reading refreshes
    sensor:status:{saloon}:{slot} with TTL = sensor_status_ttl_sec (seeded
    300). That TTL IS the LOS watchdog's 300 s grace. Buffered/replayed
    readings carry OLD device_ts — they prove history, not liveness — so they
    never refresh the key (a replay storm must never mask a real outage).
    فارسی: هر خوانشِ زنده کلیدِ وضعیت حسگر را تازه می‌کند؛ خوانشِ بازپخش هرگز.
    """
    from apps.alerts.services.redis_keys import sensor_status_key

    pipe = client.pipeline()  # one round trip for the whole live batch
    ttl = config_getter("sensor_status_ttl_sec", 300)
    for saloon_id, slot_id in pairs:
        pipe.set(sensor_status_key(saloon_id, slot_id), "ONLINE", ex=ttl)
    pipe.execute()


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
    # M4-08 counters span the WHOLE drain call, not one batch: the '>' read
    # and the PEL crash-recovery read arrive as SEPARATE batch tuples and the
    # same entry legitimately appears in both — countdown once per entry.
    buffered_by_gw: dict[str, int] = {}  # replay backlog countdown
    seen_ids: set[str] = set()
    cfg_getter = pacer._config if pacer is not None else _cfg_getter(_config_snapshot())
    for _stream, entries in batches:
        if not entries:
            continue
        by_org: dict[str, list[dict]] = {}
        ids: list[str] = []
        raw_entries: list[tuple[str, dict]] = []  # (org_id_str, fields)
        # ONE batched slot→saloon lookup per batch (was one GET per entry).
        saloon_map = _batched_saloon_map([f["slot_id"] for _, f in entries])
        # Batch the per-entry alert-evaluation reads: thresholds via ONE
        # pipelined hgetall round trip, active mutes via ONE query.
        from apps.alerts.services.redis_keys import thresholds_hash as _thr_key

        saloon_ids = {s for s in saloon_map.values() if s}
        pipe = client.pipeline()
        for sid in saloon_ids:
            pipe.hgetall(_thr_key(sid))
        thresholds_by_saloon = dict(zip(saloon_ids, pipe.execute()))
        from apps.alerts.models import MaintenanceMute
        from django.utils import timezone as _dj_tz

        mute_lookup: dict = {}
        if saloon_ids:
            for m in (
                MaintenanceMute.objects.filter(
                    saloon_id__in=saloon_ids,
                    starts_at__lte=_dj_tz.now(),
                    ends_at__gt=_dj_tz.now(),
                ).order_by("-starts_at")
            ):
                mute_lookup.setdefault(m.saloon_id, m)  # first = latest start
        for entry_id, fields in entries:
            first_sight = entry_id not in seen_ids
            seen_ids.add(entry_id)
            ids.append(entry_id)
            kwargs = _to_model_kwargs(fields)
            saloon_id = saloon_map.get(fields["slot_id"])
            if saloon_id is None:
                # Stale slot (deleted/reset): poison → DLQ with reason,
                # then ack — never crash the worker over it (Q1 family).
                from morghdari.subscriber.dlq import send_to_dlq

                send_to_dlq(
                    client,
                    fields.get("gateway_slot_id", "unknown-topic"),
                    fields.get("payload", ""),
                    "UNKNOWN_SLOT",
                )
            else:
                kwargs["saloon_id"] = saloon_id
                by_org.setdefault(kwargs["org_id"], []).append(kwargs)
                raw_entries.append((kwargs["org_id"], {"saloon_id": saloon_id, "fields": fields}))
            if fields.get("is_buffered") == "True" and first_sight:
                # Count EVERY buffered entry (even DLQ'd ones — the slot was
                # still processed), else the backlog never reaches zero.
                gw = fields.get("gateway_slot_id", "")
                buffered_by_gw[gw] = buffered_by_gw.get(gw, 0) + 1

        for org, rows in by_org.items():
            from apps.tenants.context import tenant_context

            # FORCE RLS: one GUC per commit; mixed-org batches are impossible.
            with transaction.atomic():
                with tenant_context(org):
                    SaloonTelemetry.objects.bulk_create(
                        (SaloonTelemetry(**row) for row in rows),
                        ignore_conflicts=True,  # same-entry redelivery ⇒ no-op
                    )

                    # M4-04: inline alert evaluation AFTER telemetry commit.
                    # Runs inside the SAME tenant_context for RLS consistency.
                    from apps.alerts.services.evaluation import evaluate_entry

                    for entry_org, raw in raw_entries:
                        if entry_org != org:
                            continue  # only evaluate entries belonging to THIS org
                        try:
                            metrics_data = json.loads(
                                raw["fields"].get("metrics", "{}")
                            )
                            evaluate_entry(
                                redis_client=client,
                                saloon_id=raw["saloon_id"],
                                org_id=entry_org,
                                farm_id=int(raw["fields"].get("farm_id") or 0),
                                slot_id=raw["fields"].get("slot_id", ""),
                                metrics=metrics_data,
                                device_ts=raw["fields"].get("device_ts", ""),
                                config_getter=cfg_getter,
                                mute_lookup=mute_lookup,
                                thresholds=thresholds_by_saloon.get(raw["saloon_id"]),
                            )
                        except Exception:
                            logger.exception(
                                "alert evaluation failed for saloon=%s",
                                entry_org,
                            )

            processed += len(rows)

            # M4-07: LIVE readings of THIS org refresh the LOS watchdog keys
            # ( AFTER the telemetry commit, matching the doc's pipeline order).
            live = [
                (raw["saloon_id"], raw["fields"].get("slot_id", ""))
                for entry_org, raw in raw_entries
                if entry_org == org and raw["fields"].get("is_buffered") != "True"
            ]
            if live:
                touch_sensor_status(client, live, config_getter=cfg_getter)

            if pacer is not None:
                time.sleep(pacer.sleep_seconds(org))
                pacer.wait_for_org(org)

        # THE rule: acknowledge only after the DB commit above succeeded.
        # Same entry may appear in BOTH the '>' read and the PEL read within
        # one cycle — ack counts ENTRIES, so the return value stays honest.
        if ids:
            client.xack(stream, CONSUMER_GROUP, *ids)
            acked.update(ids)

    # M4-08 (Blueprint §5): the historical backlog countdown — when the LAST
    # buffered entry of a gateway's outage drains, dispatch the retroactive
    # REPLAY sweep exactly once (replay:sweeping NX guard). Runs ONCE per
    # drain call, AFTER the batches loop (the '>' read and the PEL read are
    # separate tuples — per-batch dispatch would double-decrement), and only
    # after every batch's DB commit above.
    for gw, count in buffered_by_gw.items():
        from apps.alerts.services.replay import note_buffered_processed

        note_buffered_processed(client, gw, count)

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
    pacer = HistoricalPacer(_cfg_getter(_config_snapshot()))  # 1 query/cycle
    return drain_stream(STREAM_HISTORICAL, count, pacer=pacer)


