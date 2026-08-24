# v0.3 — Ingestion + Fake Gateway ⭐ core *(M3)*

> **Theme:** make data actually flow — the one risk that IS the product.
> **Risk killed:** the data path.
> **Maps to:** Blueprint **M3** · **Duration:** ~2 weeks

## What this version delivers

- **Subscriber service** (`python -m morghdari.subscriber`): `$share/telemetry_workers/`
  shared subscription on EMQX, topic-identity parsing (org/farm/gateway from the topic —
  never the payload), dedup guard (`SET NX EX` keyed on org+slot+device_ts, TTL 90 000 s),
  malformed-payload → `telemetry:dead_letter` with reason, envelope enrichment
  (org_id/slot_id stamped onto the stream entry), XADD to `telemetry:realtime` /
  `telemetry:historical`
- **Redis streams** with consumer groups + XACK + trimming (~100 k entries)
- **Celery consumers**: realtime worker (threshold-ready pipeline entry), historical
  worker (rate-limited 50/min aggregate, 20/org/min), batch flush (50 rows / 5 s) into
  TimescaleDB
- **`scripts/simulate_gateway.py`** fully working: real schema-2.0 payloads, buffered-
  replay mode for outage storms
- Minimal telemetry read API (`GET /saloons/{id}/telemetry/`) to prove end-to-end

## Exit demo

Simulator → EMQX → subscriber → Redis → **row visible via API** · duplicate publish →
1 row · malformed JSON → DLQ with reason · simulated 12 h outage replays through the
historical lane without delaying live messages.

## Files

[TASKS.md](TASKS.md) · [WALKTHROUGH.md](WALKTHROUGH.md) · [TUTORIAL.md](TUTORIAL.md)

## Look-back

p99 of PUBACK→Redis under the replay storm (the fsync benchmark gate)? What broke
between EMQX and Timescale that the docs didn't predict?
