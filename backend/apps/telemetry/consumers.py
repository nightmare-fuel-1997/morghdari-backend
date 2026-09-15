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
from django.db import connection, transaction
from django.utils.dateparse import parse_datetime

from apps.system_config.models import SystemConfiguration
from apps.telemetry.models import SaloonTelemetry
from morghdari.subscriber.pipeline import (
    STREAM_HISTORICAL,
    STREAM_PENDING,
    STREAM_REALTIME,
    trim_streams,
)

logger = logging.getLogger(__name__)

CONSUMER_GROUP = "telemetry_workers"   # Q2: contract name (Tracker Â§4k)
BATCH_SIZE_DEFAULT = 50
# BUG-13 (QA 17.1): after this many failed deliveries the realtime drain
# dead-letters the entry instead of re-trying it for ever.
DEFAULT_DLQ_AFTER_DELIVERIES = 5
# F4 (2026-09-10 v0.7 re-verify): PEL entries orphaned by a DEAD consumer
# (container recreation / stack swap mints a fresh SUBSCRIBER_CONSUMER_ID)
# are reclaimed by drain_stream once they have been undelivered this long.
ORPHAN_PEL_IDLE_SEC = 60

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

    v0.7 M7-06 rehearsal fix (RLS era): the entry's org is known from the
    stream fields, so the lookup runs inside tenant_context(org_id) — a bare
    PK read sees ZERO rows under FORCE ROW LEVEL SECURITY and every entry
    would dead-letter. GUC is transaction-scoped (set_config true) inside its
    own atomic block: dies with the lookup, never leaks into the batch.
    فارسی: اسلاتِ ناشناخته در زمانِ درین = پیامِ سالم به صندوق مرده، نه کرش؛
    و خواندنِ اسلات زیر RLS با GUCِ سازمانِ همان ردیف انجام می‌شود.
    """
    from apps.devices.models import SaloonSensorSlot

    def _lookup():
        try:
            return SaloonSensorSlot.objects.only("saloon_id").get(
                pk=fields["slot_id"]
            ).saloon_id
        except SaloonSensorSlot.DoesNotExist:
            return None

    if connection.vendor == "postgresql" and fields.get("org_id"):
        from apps.tenants.context import tenant_context

        with transaction.atomic(), tenant_context(fields["org_id"]):
            return _lookup()
    return _lookup()


def touch_sensor_status(client, pairs) -> None:
    """M4-07 pipeline step 5 (Walkthrough Part 3): every LIVE reading refreshes
    sensor:status:{saloon}:{slot} with TTL = sensor_status_ttl_sec (seeded
    300). That TTL IS the LOS watchdog's 300 s grace. Buffered/replayed
    readings carry OLD device_ts — they prove history, not liveness — so they
    never refresh the key (a replay storm must never mask a real outage).
    فارسی: هر خوانشِ زنده کلیدِ وضعیت حسگر را تازه می‌کند؛ خوانشِ بازپخش هرگز.
    """
    from apps.alerts.services.redis_keys import sensor_status_key

    ttl = _config_int("sensor_status_ttl_sec", 300)
    for saloon_id, slot_id in pairs:
        client.set(sensor_status_key(saloon_id, slot_id), "ONLINE", ex=ttl)


def drain_stream(stream: str, count: int = BATCH_SIZE_DEFAULT, pacer=None,
                 gateway_slot: str | None = None) -> int:
    """One consumer cycle: read NEW entries PLUS this consumer's own pending
    ones (crash recovery between read and ack), insert per-org batches,
    COMMIT, THEN ack. Returns entries processed.

    M5-05: when `gateway_slot` is given (approval drain on STREAM_PENDING),
    ONLY that gateway's entries are processed and acked — other gateways'
    held telemetry stays queued in FIFO order inside the pending stream.
    """
    client = _redis()
    _ensure_group(client, stream)
    consumer = _consumer_name()

    batches = list(
        client.xreadgroup(CONSUMER_GROUP, consumer, {stream: ">"}, count=count, block=1000)
    )
    # F4 (2026-09-10 v0.7 re-verify): reclaim PEL entries orphaned by a DEAD
    # consumer. Entries the old consumer read but never acked are invisible to
    # every live reader — observed as a permanently plateaued pending count
    # (49/40) on the rehearsal stack after a worker recreation. XAUTOCLAIM
    # moves entries idle > ORPHAN_PEL_IDLE_SEC into THIS consumer's PEL; the
    # crash-recovery read below then drains them like any redelivery
    # (justid=True: that read fetches the bodies). Live in-flight entries keep
    # a fresh idle clock and are never stolen. Skipped for the M5-05 approval
    # drain (gateway_slot filter): claiming ANOTHER gateway's held entries
    # into our PEL would strand them unacked under the filter. Clients
    # without XAUTOCLAIM (test doubles) fall back to the old behavior.
    if gateway_slot is None:
        xautoclaim = getattr(client, "xautoclaim", None)
        if xautoclaim is not None:
            try:
                xautoclaim(
                    stream,
                    CONSUMER_GROUP,
                    consumer,
                    ORPHAN_PEL_IDLE_SEC,
                    count=count,
                    justid=True,
                )
            except redis.ResponseError:
                pass  # stream/group vanished mid-cycle — next cycle re-creates
    # Crash recovery: OUR OWN pending deliveries (id "0" = the PEL).
    batches.extend(client.xreadgroup(CONSUMER_GROUP, consumer, {stream: "0"}, count=count))
    if not batches:
        return 0

    processed = 0
    acked: set[str] = set()
    _cycle_started = time.perf_counter()  # M7-04 ingestion-latency window
    # M4-08 counters span the WHOLE drain call, not one batch: the '>' read
    # and the PEL crash-recovery read arrive as SEPARATE batch tuples and the
    # same entry legitimately appears in both — countdown once per entry.
    buffered_by_gw: dict[str, int] = {}  # replay backlog countdown
    seen_ids: set[str] = set()
    # BUG-13 (QA 17.1) quarantine bookkeeping: raw fields per entry (DLQ
    # body) and the entries this cycle could not be stored - they stay
    # unacked in the PEL and are dead-lettered once they keep failing.
    fields_by_id: dict[str, dict] = {}
    failed_ids: set[str] = set()
    for _stream, entries in batches:
        if not entries:
            continue
        by_org: dict[str, list[dict]] = {}
        ids_by_org: dict[str, list[str]] = {}  # BUG-13: batch-failure attribution
        ids: list[str] = []
        raw_entries: list[tuple[str, dict]] = []  # (org_id_str, fields)
        for entry_id, fields in entries:
            if gateway_slot is not None and fields.get("gateway_slot_id") != gateway_slot:
                continue  # M5-05: another gateway's held telemetry stays put
            first_sight = entry_id not in seen_ids
            seen_ids.add(entry_id)
            ids.append(entry_id)
            fields_by_id[entry_id] = fields  # BUG-13: DLQ payload source
            try:
                kwargs = _to_model_kwargs(fields)
            except Exception:
                # BUG-13 follow-up (QA 17.1 port): unparseable fields would
                # otherwise escape drain_stream entirely — the quarantine pass
                # only sees batch-commit failures, so a malformed entry would
                # redeliver forever outside it. Dead-letter with a reason and
                # ack via the normal path — it is poison, not retryable.
                # فارسی: ورودیِ ناقص همان‌جا به صندوق مرده می‌رود، نه چرخهٔ بی‌پایان.
                from morghdari.subscriber.dlq import send_to_dlq

                logger.exception(
                    "realtime drain: unparseable entry dead-lettered (entry=%s)",
                    entry_id,
                )
                send_to_dlq(
                    client,
                    fields.get("gateway_slot_id", "unknown-topic"),
                    fields.get("payload", ""),
                    "PROCESSING_ERROR",
                )
                continue
            saloon_id = resolve_saloon(fields)
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
                ids_by_org.setdefault(kwargs["org_id"], []).append(entry_id)
                raw_entries.append((kwargs["org_id"], {"saloon_id": saloon_id, "fields": fields, "entry_id": entry_id}))
            if fields.get("is_buffered") == "True" and first_sight:
                # Count EVERY buffered entry (even DLQ'd ones — the slot was
                # still processed), else the backlog never reaches zero.
                gw = fields.get("gateway_slot_id", "")
                buffered_by_gw[gw] = buffered_by_gw.get(gw, 0) + 1
                # M7-04 observability: replay counter (Redis bridge, runbook §3.1).
                from apps.monitoring.metrics import bump_buffered

                bump_buffered(client, kwargs["org_id"])

        for org, rows in by_org.items():
            failed_here = _persist_org_batch(
                client, org, rows, raw_entries, ids_by_org.get(org, [])
            )
            if failed_here:
                # BUG-13 (QA 17.1): this org's batch could not be committed.
                # Leave those entries unacked (redelivered next cycle) and let
                # the quarantine pass below dead-letter the persistent ones.
                # فارسی: ورودیهای ناموفق در PEL میمانند و در صورت تکرار به DLQ
                # میروند - نه ackِ کورکورانه.
                failed_ids.update(failed_here)
                continue

            processed += len(rows)

            # M7-04 observability: per-(org, saloon) ingestion counters (Redis
            # bridge — runbook §3.1; silent-fail, the pipeline is not advisory).
            from apps.monitoring.metrics import bump_ingested
            from collections import Counter as _Counter

            per_saloon = _Counter(r["saloon_id"] for r in rows)
            for saloon_id, count in per_saloon.items():
                bump_ingested(client, org, saloon_id, count)

            # M7-08 MET-SAAS: the quota metering counter (Saas.md §15 —
            # org:{org_id}:meter:current_hour, TTL 3600) finally gets its real
            # bump at the doc-designated point — org known, row durable.
            from apps.monitoring.metrics import bump_meter_hour

            bump_meter_hour(client, org, len(rows))

            # M6-06: WS fan-out AFTER the commit — dashboards are only ever
            # told about DURABLE rows ("UI follows DB"). One group_send per
            # (saloon, slot, device_ts) reading; a redelivered entry after a
            # crash may re-push the same reading (at-least-once — clients
            # dedup on the (slot_id, device_ts) pair they also get via REST).
            from apps.telemetry.ws import push_telemetry_updates

            push_items = [
                {
                    "saloon_id": raw["saloon_id"],
                    "device_ts": raw["fields"].get("device_ts"),
                    "is_buffered": raw["fields"].get("is_buffered") == "True",
                    "metrics_by_slot": {
                        raw["fields"].get("slot_id", ""): json.loads(
                            raw["fields"].get("metrics", "{}")
                        )
                    },
                }
                for entry_org, raw in raw_entries
                if entry_org == org  # only push entries belonging to THIS org
            ]
            if push_items:
                push_telemetry_updates(org, push_items)

            # M4-07: LIVE readings of THIS org refresh the LOS watchdog keys
            # ( AFTER the telemetry commit, matching the doc's pipeline order).
            live = [
                (raw["saloon_id"], raw["fields"].get("slot_id", ""))
                for entry_org, raw in raw_entries
                if entry_org == org and raw["fields"].get("is_buffered") != "True"
            ]
            if live:
                touch_sensor_status(client, live)

            if pacer is not None:
                time.sleep(pacer.sleep_seconds(org))
                pacer.wait_for_org(org)

        # THE rule: acknowledge only after the DB commit above succeeded.
        # Same entry may appear in BOTH the '>' read and the PEL read within
        # one cycle — ack counts ENTRIES, so the return value stays honest.
        # BUG-13: never ack what we did not store.
        ackable = [entry for entry in ids if entry not in failed_ids]
        if ackable:
            client.xack(stream, CONSUMER_GROUP, *ackable)
            acked.update(ackable)
            if pacer is not None:
                time.sleep(pacer.sleep_seconds(org))
                pacer.wait_for_org(org)

    # M7-04 observability: end-to-end ingestion latency for this drain cycle
    # (read → commit). Advisory histogram — never alert-critical.
    from apps.monitoring.metrics import note_ingestion_latency

    note_ingestion_latency(client, time.perf_counter() - _cycle_started)

    # M4-08 (Blueprint §5): the historical backlog countdown — when the LAST
    # buffered entry of a gateway's outage drains, dispatch the retroactive
    # REPLAY sweep exactly once (replay:sweeping NX guard). Runs ONCE per
    # drain call, AFTER the batches loop (the '>' read and the PEL read are
    # separate tuples — per-batch dispatch would double-decrement), and only
    # after every batch's DB commit above.
    # BUG-13 (QA 17.1): after the batch loop, dead-letter any entry that has
    # crossed the delivery threshold (it was deliberately left unacked above).
    # فارسی: قرنطینهٔ ورودیهای پیاپی-ناموفق با DLQ + ack تا حلقهٔ خطا بسته شود.
    if failed_ids:
        acked.update(
            _quarantine_repeated_failures(client, stream, failed_ids, fields_by_id)
        )

    for gw, count in buffered_by_gw.items():
        from apps.alerts.services.replay import note_buffered_processed

        note_buffered_processed(client, gw, count)

    trim_streams(client)  # M3-07: bounded streams on every touch
    return len(acked)  # unique entries acknowledged; row count may be lower


def _evaluate_org_entries(client, org, raw_entries, only_ids=None):
    """M4-04: inline alert evaluation for one org's entries (post-commit).

    Extracted from drain_stream for BUG-13 so a poisoned batch can re-evaluate
    only the rows it actually managed to store.
    """
    from apps.alerts.services.evaluation import evaluate_entry

    for entry_org, raw in raw_entries:
        if entry_org != org:
            continue  # only evaluate entries belonging to THIS org
        if only_ids is not None and raw.get("entry_id") not in only_ids:
            continue
        try:
            metrics_data = json.loads(raw["fields"].get("metrics", "{}"))
            _eval_started = time.perf_counter()
            evaluate_entry(
                redis_client=client,
                saloon_id=raw["saloon_id"],
                org_id=entry_org,
                farm_id=int(raw["fields"].get("farm_id") or 0),
                slot_id=raw["fields"].get("slot_id", ""),
                metrics=metrics_data,
                device_ts=raw["fields"].get("device_ts", ""),
            )
            # M7-04 observability: eval latency ring (runbook 3.1).
            from apps.monitoring.metrics import note_alert_eval_latency

            note_alert_eval_latency(client, time.perf_counter() - _eval_started)
        except Exception:
            logger.exception("alert evaluation failed for saloon=%s", entry_org)


def _persist_org_batch(client, org, rows, raw_entries, entry_ids) -> set:
    """Insert one org's telemetry batch + inline eval; return UNSTORED entry ids.

    BUG-13 (QA 17.1): the realtime drain had NO poison-message quarantine. An
    entry that raised during processing (observed: a NUMERIC(10,2) overflow from
    an absurd recv_ts, but any unprocessable shape qualifies) was retried on
    EVERY drain cycle for ever - 47 deliveries observed - with a stack trace
    every 2s, and was never dead-lettered. On batch failure the rows are
    therefore retried ONE BY ONE so a single poison entry is quarantined
    without discarding the healthy rows that shared its batch.
    فارسی: در صورت خطا ردیفها یکبهیک درج میشوند تا فقط پیامِ سمی قرنطینه شود و
    دادهٔ سالم از دست نرود.
    """
    from apps.tenants.context import tenant_context

    try:
        with transaction.atomic():
            with tenant_context(org):
                SaloonTelemetry.objects.bulk_create(
                    (SaloonTelemetry(**row) for row in rows),
                    ignore_conflicts=True,  # same-entry redelivery -> no-op
                )
                _evaluate_org_entries(client, org, raw_entries)
        return set()
    except Exception:
        logger.exception(
            "realtime drain: batch insert failed for org=%s - isolating rows", org
        )

    failed: set = set()
    stored_ids: set = set()
    for index, row in enumerate(rows):
        entry_id = entry_ids[index] if index < len(entry_ids) else None
        try:
            with transaction.atomic():
                with tenant_context(org):
                    SaloonTelemetry.objects.bulk_create(
                        [SaloonTelemetry(**row)], ignore_conflicts=True
                    )
        except Exception:
            logger.exception(
                "realtime drain: poison entry quarantined (org=%s entry=%s)",
                org, entry_id,
            )
            if entry_id is not None:
                failed.add(entry_id)
        else:
            if entry_id is not None:
                stored_ids.add(entry_id)

    if stored_ids:
        try:
            with transaction.atomic():
                with tenant_context(org):
                    _evaluate_org_entries(client, org, raw_entries, only_ids=stored_ids)
        except Exception:
            logger.exception("realtime drain: post-isolation eval failed org=%s", org)
    return failed


def _quarantine_repeated_failures(client, stream, failed_ids, fields_by_id) -> set:
    """DLQ + XACK entries whose delivery count crossed the quarantine limit.

    BUG-13 (QA 17.1). Returns the entry ids actually quarantined this cycle.
    The threshold is config-driven (`realtime_dlq_after_deliveries`) with a
    sane default so an operator can tune it without a code change.
    فارسی: پیامهایی که چند بار پیاپی شکست خوردهاند با دلیل به DLQ میروند و ack
    میشوند تا حلقهٔ بیپایانِ خطا متوقف شود.
    """
    if not hasattr(client, "xpending_range"):
        return set()  # test double without XPENDING support
    threshold = max(
        _config_int("realtime_dlq_after_deliveries", DEFAULT_DLQ_AFTER_DELIVERIES), 1
    )
    quarantined: set = set()
    for entry_id in sorted(failed_ids):
        try:
            pending = client.xpending_range(
                stream, CONSUMER_GROUP, min=entry_id, max=entry_id, count=1
            )
        except Exception:
            logger.exception("BUG-13: XPENDING failed for entry %s", entry_id)
            continue
        deliveries = 0
        if pending:
            row = pending[0] or {}
            deliveries = row.get("times_delivered") or row.get("delivery_count") or 0
        if deliveries < threshold:
            continue
        fields = fields_by_id.get(entry_id, {})
        try:
            from morghdari.subscriber.dlq import send_to_dlq

            send_to_dlq(
                client,
                fields.get("gateway_slot_id", "unknown-topic"),
                fields.get("payload", ""),
                "PROCESSING_FAILED",
            )
        except Exception:
            logger.exception("BUG-13: DLQ write failed for entry %s", entry_id)
            continue
        client.xack(stream, CONSUMER_GROUP, entry_id)
        logger.warning(
            "BUG-13: quarantined entry %s after %s deliveries", entry_id, deliveries
        )
        quarantined.add(entry_id)
    return quarantined


def discard_pending_for_gateway(client, gateway_slot: str, reason: str = "gateway_rejected") -> int:
    """M5-05 reject path: archive a rejected gateway's held telemetry to the
    DLQ (with a reason) and remove it from the pending stream — discarded but
    never silently lost. Returns the number of entries archived."""
    from morghdari.subscriber.dlq import send_to_dlq

    entries = list(client.xrange(STREAM_PENDING, min="-", max="+"))
    victims = [
        (entry_id, fields)
        for entry_id, fields in entries
        if fields.get("gateway_slot_id") == gateway_slot
    ]
    for entry_id, fields in victims:
        send_to_dlq(
            client,
            fields.get("gateway_slot_id", "unknown-topic"),
            fields.get("payload", ""),
            reason,
        )
    if victims:
        client.xdel(STREAM_PENDING, *(entry_id for entry_id, _ in victims))
    return len(victims)


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


