# v0.4 — Alert Engine *(M4)*

> **Theme:** the safety brain — alerts that fire when they must and never when they
> must not. **Risk killed:** safety logic.
> **Maps to:** Blueprint **M4** · **Duration:** ~2 weeks · **Suite ≥90 % coverage**

## What this version delivers

- Threshold cache per saloon/day (midnight cron + immediate re-cache on flock changes)
- Breach accumulation keyed with TTL-refresh (the 15-min alert CAN fire — regression-
> tested), hysteresis resolution (+0.5 units), midnight-transition grace 300 s
- NH₃ velocity bypass (`max_ts ≥ 50` → instant Tier 1, skips duration)
- 3-tier escalation loop (2-min sweep; Tier 2 at 15 min, Tier 3 at 30): worker push →
> engineer SMS → manager+doctor; acknowledgement pause key (`alert:paused:{id}`, TTL
> from config) — the ack MECHANISM lands here, the REST endpoint at v0.6
- LOS watchdog (60 s sweep, 300 s grace), replay sweep (never pushes, labels historical)
- **Capability-gated rules (D23):** THI derived alerts only where temp+humidity are
> live; `co`/`power_*` rules evaluate only where sensors exist
- **GS-HUMAN-SAFETY REJECTED** (owner, 2026-08-23 — Tracker §4i): no fixed baselines;
  alerting remains flock-gated, velocity emergencies still bypass every suppression
  layer (Decision 10). **Lands instead:** farm power-outage correlation rule
  (GS-CORRELATION) + maintenance-mute windows (GS-MAINT-MUTE)
- MET-ALERT-QUALITY numbers recorded at retrospective (MTTR, ack-to-resolve, alerts/flock)

## Exit demo

Scripted NH₃ spike (max_ts 57) → Tier 1 <2 s bypassing everything · sustained temp
breach → Tier 1 exactly at 15 min → ack pauses → Tier 2/3 timeline · resolution only
past hysteresis · sensor-less saloon shows THI unavailable-with-reason, never a false
reading.

## Files

[TASKS.md](TASKS.md) · [WALKTHROUGH.md](WALKTHROUGH.md) · [TUTORIAL.md](TUTORIAL.md)

## Look-back

Did any path let Redis loss silently disable alerting again? Alert quality in NUMBERS
(MET-ALERT-QUALITY).
