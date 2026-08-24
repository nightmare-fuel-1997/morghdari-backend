# v0.3 TUTORIAL — build the ingestion pipeline yourself

> ⚠️ **Pre-spiral draft** — re-validate at spiral start. **How to use:** 1–4 teach,
> 5 is hands-on, 6 warns, 7 checks. KB lessons: *The Data Journey* (#/data-journey),
> *MQTT & Messages* (#/mqtt). Canonical mechanics: Saas.md §4/§14, Walkthrough §1–2.

---

## 1. The Mission

Connect a fake gateway to a real TimescaleDB row through the exact production path:
MQTT → subscriber → Redis Streams → Celery → batch insert. This path IS the product —
alerts (v0.4), dashboards (v0.6) and the business (v1.0) are all downstream consumers
of what you build here.

**Done means:** simulator → row via API · duplicate → 1 row · bad JSON → DLQ with
reason · a simulated 12-hour outage replays without delaying live traffic.

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| `$share/telemetry_workers/` shared subscription | N subscribers split one message stream | Scaling + the T4b safeguard | Never plain subscribe in prod — every subscriber would double-process |
| `clean_session=False` + 24 h expiry | EMQX queues for the subscriber offline | Survives broker restarts | Not a data store — capacity ≈ mqueue_len ÷ fleet rate (T4b math) |
| `SET NX EX 90000` dedup guard | First writer wins per (org,slot,device_ts) | QoS-1 delivers at-least-once; guard makes it effectively-once | TTL < session expiry would allow late redeliveries through |
| `telemetry:dead_letter` stream | Poison payloads with reason | Forensics without crashing | Not a trash fire — entries carry topic+payload+reason |
| Redis Streams + consumer groups | Durable work queue with ACK | At-least-once delivery to workers | Don't XACK before DB commit |
| Batch flush (50 rows / 5 s) | bulk_create to Timescale | Timescale hates row-by-row inserts | Not instant — durability SLO is 30 s, not 0 |
| `simulate_gateway.py` | Honest test bench producing schema-2.0 payloads | Rehearses storms before real radios | Not a fixture generator — timestamps must be realistic |

---

## 3. The Build

### 3.1 Topic identity — the transport is the truth

```
{org_id}/farms/{farm_id}/gateways/{gateway_slot_id}/telemetry
```

```python
org_id, farm_id, gateway_slot = parse_topic(topic)   # regex, strict
if payload["gateway_slot_id"] != gateway_slot:
    dlq(msg, "TOPIC_PAYLOAD_IDENTITY_MISMATCH")      # R2 cross-check
```

The subscriber parses org/farm/gateway **from the topic** — the broker authenticated
the connection, so the path is trusted. The one payload field that mirrors the topic,
`gateway_slot_id`, must MATCH it (mismatch = identity fault → DLQ). There is NO pack
level in the topic: each reading carries its `pack_id` inside the payload, and
`pack_id → slot_id` resolves via `gateway_sensor_mapping` (cache first, DB fallback,
DLQ as `UNKNOWN_SLOT` on miss).

### 3.2 The subscriber connection

```python
client = mqtt.Client(client_id=SUBSCRIBER_CLIENT_ID, clean_session=False)  # e.g. "subscriber-01"
client.subscribe("$share/telemetry_workers/+/farms/+/gateways/+/telemetry", qos=1)
```

Line by line: unique `client_id` from env (identical ids = infinite kick-loop — the
original CRITICAL audit finding); `clean_session=False` keeps the EMQX session (and its
queue) alive across restarts; the `$share/` prefix joins the worker group.

### 3.3 Dedup — cheap check before expensive work

```python
key = f"dedup:{org_id}:{slot_id}:{device_ts}"
if not redis.set(key, 1, nx=True, ex=90000):
    return                                    # duplicate — drop silently
```

90000 s = 25 h ≥ 24 h session expiry (the AU-fix invariant): a redelivery arriving
after a long outage STILL hits the guard. The DB `UNIQUE(server_received_ts, slot_id,
org_id)` is partition-integrity only — `server_received_ts = NOW()` differs per
delivery, so it can never dedup. Redis guards; Postgres partitions.

### 3.4 Enrich → XADD, or DLQ

```python
try:
    payload = json.loads(msg)
except ValueError:
    redis.xadd("telemetry:dead_letter",
               {"topic": topic, "payload": msg, "reason": "bad_json"})
    return

redis.xadd("telemetry:realtime", {
    "org_id": org_id, "farm_id": farm_id,
    "gateway_slot_id": gateway_slot, "device_ts": payload["device_ts"],
    "is_buffered": payload["is_buffered"], "slot_id": slot_id,
    "payload": raw_json, "recv_ts": time.time(),
})
```

The stream entry carries the enriched fields the worker reads — the D14 audit fix
exists because a bare payload made workers KeyError. Note what is NOT here: `saloon_id`
— the WORKER derives it from the resolved slot; and there is one `device_ts` per
PAYLOAD (the 60s window start), which is also the dedup key's third component. DLQ
entries always carry a reason.

### 3.5 Two consumers, two personalities

```python
# realtime — latency-sensitive, no rate limit
entry = redis.xreadgroup("workers", CONSUMER, {"telemetry:realtime": ">"},
                         count=10, block=5000)
...validate → insert buffer → XACK after DB commit

# historical — polite under storms
rate = config("replay_rate_limit_per_min")            # 50; per-org 20
```

Realtime serves dashboards and (later) alerts. Historical drains buffered replay at
config-capped rates so one tenant's storm can't starve live traffic (noisy-neighbor
fairness). **XACK after commit:** a crash between = redelivery; dedup makes redelivery
harmless. At-least-once + idempotency = effectively-once.

### 3.6 Batch flush

```python
if len(buffer) >= 50 or seconds_since_flush >= 5:
    Telemetry.objects.bulk_create(buffer)             # NEVER row-by-row
    buffer.clear()
```

TimescaleDB performance collapses under chatty single-row inserts. 50 rows or 5 s —
whichever first; durability SLO (30 s) has margin by design.

### 3.7 Trimming — availability, not housekeeping

```python
redis.xtrim("telemetry:realtime", maxlen=100_000, approximate=True)
```

`maxmemory 1500mb noeviction` means an untrimmed stream eventually rejects ALL writes
— including alerts. Trim on the write path, always.

### 3.8 The simulator — schema-2.0 honesty

```json
{"gateway_hardware_id": "AA:BB:CC:DD:EE:FF",
 "gateway_slot_id": "FARM-01-GW-A",
 "schema_version": "2.0",
 "device_ts": "2026-08-16T10:30:00Z",
 "is_buffered": false,
 "clock_drift": false,
 "readings": [
   {"pack_id": "P-A",
    "metrics": {"temp": {"avg": 28.4, "min": 27.1, "max": 29.2},
                "nh3":  {"avg": 14.2, "min": 11.0, "max": 22.0, "max_ts": 58}}}
 ]}
```

Every field is load-bearing: `schema_version` gates the parser; `gateway_hardware_id`
drives pack→slot resolution and registry cross-validation; `gateway_slot_id` must equal
the topic segment (mismatch → DLQ); exactly ONE top-level `device_ts` per payload — the
60s window start and the dedup key's third component; `is_buffered` selects the lane;
`max_ts` is NH₃-only. No slot ids, no saloon ids on the wire — the backend resolves
those. `--replay-hours N` emits N hours of buffered payloads (`is_buffered: true`, OLD
timestamps), then resumes live — your rehearsal for outage storms and your p99
measurement harness (PUBACK→Redis, the fsync benchmark gate).

### 3.9 Minimal read API — close the loop

`GET /saloons/{id}/telemetry/?since=&until=&include_buffered=` — RLS-scoped like
everything else. When curl shows YOUR simulated row, the core risk is dead.

---

## 4. Cheat Sheet

| Command | Purpose | Key detail | Gotcha |
|---|---|---|---|
| `python -m morghdari.subscriber` | Run the bridge | unique client id env | same id twice = kick-loop |
| `XLEN telemetry:realtime` | Queue depth | grows under load | unbounded = misconfigured trim |
| `XREVRANGE telemetry:dead_letter + - COUNT 1` | Inspect newest DLQ entry | carries reason | don't XTRIM without archiving |
| `XPENDING telemetry:realtime workers` | Stuck deliveries | PEL grows if no XACK | crash-before-ACK is safe (dedup) |
| `simulate_gateway.py --replay-hours 12` | Outage storm | old timestamps | watch the live lane stay fast |
| `redis-cli -n 0 XINFO GROUPS telemetry:realtime` | Consumer group state | lag + pending | group name is part of the contract |

---

## 5. Your Subtasks

1. **Boot dev compose; create the EMQX dev user.** Verify: subscriber connects (log
   line). Notice: dev auth is built-in-database, prod is PostgreSQL — never mix.
2. **Write the topic parser + its unit tests first** (valid, malformed, forged). Verify:
   forgery test red→green. Notice: you wrote security before throughput.
3. **Implement dedup; write the double-publish test.** Verify: 1 row, not 2. Notice:
   the test fails if you move dedup AFTER enrichment — order matters.
4. **Add DLQ path; feed it garbage.** Verify: entry carries `reason: bad_json`.
   Notice: nothing crashed — that's the design.
5. **Build the realtime consumer with XACK-after-commit.** Verify: kill -9 mid-batch →
   redelivery → still 1 row. Notice: dedup turned a crash into a no-op.
6. **Add the batch flush; measure.** Verify: 50-row flush visible in logs. Notice:
   latency to row ≈ seconds, comfortably inside the 30 s SLO.
7. **Add the historical lane + rate limits from system_configuration.** Verify: config
   change alters drain speed without redeploy. Notice: D21 config-is-data working.
8. **Add trimming; run a sustained-load loop.** Verify: XLEN plateaus ≈ 100 k. Notice:
   memory stays flat.
9. **Build the simulator + replay mode.** Verify: storm drains at ≤50/min while a
   simultaneous live reading lands instantly. Notice: two lanes, zero coupling.
10. **Wire the read API; curl your own row.** Verify: end-to-end in one screenshot.
    Notice: this is the first moment the product exists.

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| XACK before DB commit | Crash loses the batch silently |
| Dedup after enrichment | Wasted work + race duplicates |
| Trusting payload org/farm fields | Forged cross-tenant telemetry accepted |
| sqlite for integration tests | Every Timescale behavior hidden until prod |
| Row-by-row inserts | Insert latency explodes under the first storm |
| Shared subscription tested with ONE subscriber | Scaling claim never actually tested |

---

## 7. Prove It

1. QoS-1 guarantees at-least-once. Name the TWO mechanisms that turn that into
   effectively-once, and where each runs. *(§3.3/§3.5.)*
2. Why must the dedup TTL be ≥ the EMQX session expiry? Give the failure timeline if
   it were shorter. *(§3.3.)*
3. A payload's JSON says `org_id=B` but the topic says `org_id=A`. What happens, and
   which principle decided that? *(§3.1.)*
4. The historical lane is rate-limited but the realtime lane is not. What failure does
   this asymmetry prevent? *(§3.5.)*
5. Where does the DB unique constraint fit if it can't dedup? *(§3.3.)*
