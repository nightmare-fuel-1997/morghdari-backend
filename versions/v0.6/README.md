# v0.6 — Full REST + WebSocket *(M6)*

> **Theme:** the complete live UX contract. **Risk killed:** live UX.
> **Maps to:** Blueprint **M6** · **Duration:** ~2 weeks

## What this version delivers

- **All 32 tenant endpoints** (v1.1 operability set + 5 v1.3 KPI/dashboard + 3 v1.4
  water/biosecurity/capabilities) — OpenAPI **v1.5.0** diff-tested `implemented == contract`
- **Scope extensions (additive, Tracker §4o/§4p):** Flutter compat adapter
  (contract v1.6.0-draft — `/api/compat/v1` + legacy aliases, `legacy_slug_map`) and
  ops read APIs (contract v1.7.0-draft — pagination convention, audit-logs, thresholds,
  photos, gateway reads, password change + sessions)
- **Channels consumer**: per-saloon `telemetry.update` pushes, ws:active Set cap
  (config, TTL-refreshed), close codes 4003 (auth/org) / 4000 (cap), reconnect gap-fill
- **KPI computation service** (D22): mortality %, compliance %, FCR (NULL-until-inputs),
  cycle summary, water:feed ratio — computed per request
- **Dashboard-config toggle resolution** (personal → role default → system default)
- **Capability profile service**: `GET /saloons/{id}/capabilities/` (D23) —
  available / unavailable + missing inputs + enable path

## Exit demo

OpenAPI diff test green · browser receives `telemetry.update` every 60 s from the
simulator · reconnect gap-fill merges cleanly · capabilities endpoint reports FCR
unavailable-with-reason, then available after one weight sample.

## Files

[TASKS.md](TASKS.md) · [WALKTHROUGH.md](WALKTHROUGH.md) · [TUTORIAL.md](TUTORIAL.md)

## Look-back

WS message schema stable enough to freeze for the Frontend Integration Spec (FE-SPEC)?
