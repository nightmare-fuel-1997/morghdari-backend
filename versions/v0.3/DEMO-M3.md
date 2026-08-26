# v0.3 EXIT DEMO — ingestion + fake gateway (recorded 2026-08-26)

> EN: Scripted transcript per WALKTHROUGH order, ending on the storm — the
> noisy-neighbour payoff. Outputs are the exact shapes asserted by
> `backend/tests/test_m3_0{2,5,8,9,10}_*.py` (130 passed @ 87.37% in the
> isolated CI gate).
>
> فارسی: رونوشت دموی خروج نسخهٔ ۰٫۳؛ پایانِ دمو طوفانِ پخشِ مجدد است.

## Step 1 — subscriber boots and binds the group

```
$ python -m morghdari.subscriber   # env: SUBSCRIBER_CLIENT_ID=subscriber-01 ...
INFO morghdari.subscriber: connected to broker (session_present=False)
INFO morghdari.subscriber: subscribed via $share/telemetry_workers/+/farms/+/gateways/+/telemetry (qos=1)
```

Missing `SUBSCRIBER_CLIENT_ID` refuses boot (exit 2) with the reason printed.
Duplicate id ⇒ displaced instance logs a visible disconnect.

## Step 2 — live reading through the whole path

```
$ python scripts/simulate_gateway.py --gateway FARM-01-GW-A --org <uuid> --interval 2
[live] 2026-08-26T10:00:00Z → 11111111-.../farms/12/gateways/FARM-01-GW-A/telemetry
```
```bash
curl -s "localhost:8000/api/v1/saloons/<saloon_id>/telemetry/" -H "Authorization: Bearer <owner-token>"
# {"saloon_id":"<id>","results":[{"device_ts":"...","metrics":{"temp":{"avg":28.4,...}},"buffered_delay_mins":0.4,...}]}
```

## Step 3 — duplicate is a no-op

Republish the identical payload → row count stays **1** (per-slot NX dedup;
duplicate drops silently as a SUCCESS).

## Step 4 — poison goes to DLQ with a reason

```bash
# malformed JSON via simulator debug hook or manual mosquitto_pub:
redis-cli XREVRANGE telemetry:dead_letter + - COUNT 1
# {"topic": "...", "payload": "{not json", "reason": "bad_json"}
```

Other reasons proven by tests: `TOPIC_PAYLOAD_IDENTITY_MISMATCH`,
`UNSUPPORTED_SCHEMA_VERSION`, `UNKNOWN_SLOT`, `GATEWAY_NOT_PROVISIONED` (Q1).

## Step 5 — the storm (noisy-neighbour payoff)

```
$ python scripts/simulate_gateway.py ... --replay-hours 4 --interval 0.5
[buffered] 2026-08-26T06:00:00Z → ...      ← 240 windows of backlog
[buffered] 2026-08-26T06:01:00Z → ...
...
[live] 2026-08-26T10:00:00Z → ...          ← live resumes instantly after backlog queue
```

While the historical lane drains at ≤50/min (config-capped), a fresh LIVE
reading lands in the API immediately — two lanes, zero coupling.

```
XLEN telemetry:realtime → 20   XLEN telemetry:historical → drains to 0
XPENDING telemetry:realtime workers → 0   (acked strictly after commit)
rows: 260 total = 240 buffered + 20 live · full identical replay ⇒ still 240+20
```

## Gates at close

- Isolated CI gate: `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build test`
  → **130 passed @ 87.37%**
- Tag: `v0.3` (annotated)
