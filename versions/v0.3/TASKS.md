# v0.3 TASKS — ordered breakdown

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M3-01 | `requirements`: paho-mqtt into subscriber extras; subscriber entrypoint `python -m morghdari.subscriber` | v0.2 | module boots, connects to dev EMQX |
| M3-02 | Topic parser (org/farm/gateway from TOPIC; payload `gateway_slot_id` cross-checked, mismatch → DLQ) + slot resolver (`gateway_sensor_mapping`, cache-first; pack_id comes from each payload reading) | M3-01 | unit tests: parse/resolve/mismatch-DLQ |
| M3-03 | Dedup guard (`SET NX EX 90000`) + DLQ writer (payload+reason) | M3-02 | duplicate dropped; bad JSON lands in DLQ with reason |
| M3-04 | XADD enrichment contract (org_id/farm_id/gateway_slot_id/device_ts/is_buffered/slot_id/payload/recv_ts — NO saloon_id; the worker derives it from the resolved slot) to realtime/historical streams | M3-03 | stream entries carry enriched fields |
| M3-05 | Celery consumer group + XACK-after-commit + batch flush (50 rows / 5 s, bulk_create) | M3-04 | payload → row in Timescale via CI image |
| M3-06 | Historical lane: rate limits read from system_configuration (50/min agg, 20/org/min) | M3-05 | replay storm never delays live lane |
| M3-07 | Stream trimming (~100 k approximate) on write path | M3-04 | XLEN bounded under sustained load test |
| M3-08 | `simulate_gateway.py` full: schema-2.0 payloads, `--replay-hours`, multi-pack | M1-10 | storm mode produces buffered replay |
| M3-09 | Minimal telemetry read API per OpenAPI (RLS-scoped) | M3-05 | curl shows simulated reading |
| M3-10 | Integration suite vs timescale image: e2e / dedup / DLQ / replay-lane separation | all | suite green in CI |
| M3-11 | DoD sweep: CHANGELOG, tag `v0.3`, demo recorded (storm!), docs swept | all | tag pushed |
