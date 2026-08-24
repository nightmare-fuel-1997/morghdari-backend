# v0.3 WALKTHROUGH — the first data flows

Prereqs: v0.2 complete; dev compose up (emqx/redis/postgres healthy).

## 1. Start the pipeline

```bash
celery -A config worker -Q telemetry_realtime,telemetry_historical -l info   # consumers (canonical queue names — Blueprint §5)
python -m morghdari.subscriber                              # MQTT → Redis bridge
```

## 2. Ship a reading

```bash
python scripts/simulate_gateway.py --gateway GW-001 --interval 60
```

Watch: EMQX dashboard (:18083) shows the client; subscriber logs the topic parse +
dedup verdict; `XLEN telemetry:realtime` grows.

## 3. Prove the row (end-to-end)

```bash
curl -s "localhost:8000/api/v1/saloons/1/telemetry/?since=<ts>" -H "Authorization: Bearer <token>"
```

A reading with dual timestamps and `buffered_delay_mins` near 0. **That curl is the
product's heartbeat.**

## 4. Dedup — QoS-1 honesty

Republish the identical payload (same device_ts): row count stays 1. The dedup key
(org,slot,device_ts) did its job; the DB unique constraint is partition-integrity only.

## 5. DLQ — failure with a paper trail

Publish malformed JSON → subscriber logs DLQ write; inspect:

```bash
redis-cli XREVRANGE telemetry:dead_letter + - COUNT 1   # payload + reason
```

## 6. The outage storm — historical lane

Stop the simulator for "12 hours" (or use `--replay-hours 12`), restart it: buffered
payloads drain through `telemetry:historical` at ≤50/min (≤20 per org) while fresh
live readings continue landing in realtime. Observe the two lanes never block each
other.

Demo script (record): 2 → 3 → 4 → 5 → 6. Step 6 is the money shot.
