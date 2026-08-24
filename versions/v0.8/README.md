# v0.8 — DR + Real Hardware HIL *(M8)*

> **Theme:** prove recovery and close the gap between simulated and real radios.
> **Risk killed:** disaster + simulated-vs-real.
> **Maps to:** Blueprint **M8** · **Duration:** ~2 weeks

## What this version delivers

- wal-g baked into the postgres image (`archive_mode=on`, WAL upload, nightly base
> backup) — PITR becomes REAL instead of documented
- T2 drill (PostgreSQL corruption → point-in-time recovery) rehearsed on staging,
> first entry in the DR Testing Log
- T3 drill (catastrophic Redis wipe): AOF replay path + `rebuild_active_state_from_db` +
> threshold/zone cache warmers + LOS suppression — alerting fully re-armed in-window
- Hardware-in-the-loop: real ESP32+LoRa gateway running the firmware spec, one sensor
> pack; zero-touch on real radios
- Power-cut test: mains cut to the gateway → FIFO replay with ORIGINAL timestamps

## Exit demo

PITR restore drill logged (RTO vs plan) · Redis-wipe drill recovers alerting within
the window · real gateway provisions zero-touch, streams real readings, survives a
real power cut.

## Files

[TASKS.md](TASKS.md) · [WALKTHROUGH.md](WALKTHROUGH.md) · [TUTORIAL.md](TUTORIAL.md)

## Look-back

How long did restore actually take vs RTO? What did the real radio teach that the
firmware spec got wrong? (Findings sweep into docs + Tracker.)
