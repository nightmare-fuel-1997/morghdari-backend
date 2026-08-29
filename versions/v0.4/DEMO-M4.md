# v0.4 EXIT DEMO — alert engine (recorded 2026-08-29)

> EN: Scripted transcript per WALKTHROUGH order. Numbers are the exact shapes
> asserted by `backend/tests/test_m4_*.py` (215 passed @ 88.64% on the local
> Postgres gate; SQLite fallback 117 passed with Postgres-required tests skipped).
>
> فارسی: رونوشت دموی خروج نسخهٔ ۰٫۴؛ پایانِ دمو MET-ALERT-QUALITY شمارنده‌هاست.

## Step 1 — the emergency (velocity bypass)

```
$ python scripts/simulate_gateway.py --gateway FARM-01-GW-A --org <uuid> --nh3-max-ts 57
[live] nh3 max_ts=57 → .../farms/12/gateways/FARM-01-GW-A/telemetry
INFO apps.alerts.services.firing: alert fired: saloon=12 metric=nh3 value=57
```

Tier 1 push fires < 2 s — before any duration logic could. `alert_instances` row:
`source=REALTIME`, `current_value=57`, `current_status=ACTIVE_TIER_1`.

## Step 2 — the textbook breach (15 minutes exactly)

Inject temp above threshold continuously; the breach key TTL refreshes each
reading. At elapsed=900 s Tier 1 fires exactly once:

```
INFO apps.alerts.services.firing: alert fired: saloon=12 metric=temp value=31.2
INFO apps.alerts.services.sweeps: escalation tier=2 saloon=12 metric=temp
```

Acknowledge (mechanism: row + pause key in one atomic block) → escalation timer
freezes; pause expires → Tier 2 SMS to engineer, then Tier 3 to manager+doctor.
Let the value drop 0.5 units inside the threshold ⇒ RESOLVED, no flapping.

## Step 3 — hysteresis — no flapping

Oscillate temp right at the threshold: alerts resolve only after the value drops
0.5 units inside. Count rows: one alert, one resolution.

## Step 4 — LOS — silence detected

Stop the gateway simulator: within ~60 s + 300 s grace the watchdog opens ONE
saloon-scoped Tier-1 LOS row (routed installer+hardware_team). Restart Redis
mid-demo → the sweep stays silent for `los_suppress_after_redis_restart_min`=10
instead of a false fleet-wide page.

## Step 5 — capability gating — polite unavailability

A saloon whose packs lack humidity: THI rules never evaluate; the engine logs
"THI unavailable — no humidity source", and nothing false ever fires. Add a
humidity pack → THI arms itself without redeploy. A saloon without a `co` pack:
`co` rules stay structurally inert.

## Step 6 — replay sweep — history without panic

Run a replay storm batch (v0.3 simulator, `--replay-hours 4`): afterwards
`REPLAY_ANALYSIS` alert rows exist with `batch_id`, none of them pushed
notifications; re-running the same replay batch is a no-op (idempotent batch_id).

## Step 7 — GS-CORRELATION + maintenance mute

Kill power to a whole farm: ≥ 0.7 of the farm's ACTIVE slots go LOS within 3 min
⇒ ONE consolidated farm-wide page; per-sensor rows still log/alert-instance;
further paging suppressed for `correlation_suppress_minutes`. Open a
maintenance_mutes window: threshold/duration alerts go silent (velocity still
pages), and the escalation sweep writes ONE structured digest line per ended
window.

## Step 8 — MET-ALERT-QUALITY (the payoff)

```python
from apps.alerts.services.quality import snapshot
snapshot()
# {
#   "mttr_seconds_by_tier": {1: 812.4, 2: 1103.0},
#   "ack_to_resolve_seconds": {"acked_mean_seconds": 640.2, "never_acked_mean_seconds": 1210.7},
#   "alerts_per_flock": [{"flock_id": "...", "saloon_id": 12, "alerts": 14, "mortality_pct": 2.3}, ...]
# }
```

These counters go into the retrospective — the same definitions will render in
Grafana (M7) via the same functions.

## Exit gate

- 215 passed @ 88.64% (Postgres mode); SQLite fallback 117 passed
- quality.py 98%; M4-11 acceptance: 9/9 quality tests green, numbers recorded in
  CHANGELOG
