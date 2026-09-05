# v0.6 WALKTHROUGH — the living dashboard

Prereqs: v0.5 running; simulator flowing; two browser tabs (or wscat ×2).

## 1. Contract = reality

Run the diff test in CI — green. Add a fake endpoint locally → red. Delete it. That's
the guardrail demoing itself.

## 2. Live updates, two windows

Open two browsers on saloon 1's dashboard (authed as different users). Simulator ticks
→ BOTH update together within seconds. Third connection beyond the user cap → closed
**4000** (cap); a cross-org user → closed **4003** (auth/org).

## 3. The gap-fill dance

Kill wifi mid-stream (stop simulator), note last seen timestamp, restart: REST
backfill merges with live pushes — no duplicates, no holes.

## 4. KPIs come alive

Log weight samples + feed intake + water intake for an active flock →
`GET /saloons/1/kpis/` returns real FCR/mortality/compliance/cycle-summary. Before the
first weight sample: `"fcr": null` + unavailable reason (D22/D23 in action).

## 5. Capabilities — the honest machine

`GET /saloons/2/capabilities/` on a sensor-less saloon: THI/water-ratio listed
unavailable with missing inputs + enable path. Log one water entry → water:feed ratio
flips available.

## 6. The Flutter app connects (v1.6 compat)

Point the SHIPPED Flutter build at the backend unchanged: its legacy `/api/...`
calls and `/ws/farms/{farmSlug}/telemetry` all answer through the compat adapter —
org/farm/device lists, latest + history with the exact flat keys
(`temperature`/`humidity`/`lux`/`nh3`, `{"farm_history": [...]}`), live flat-record
push. The dashboard just works, no rewrite.

## 7. Ops reads (v1.7)

As manager: `GET /api/v1/audit-logs/` → `{results, count}`. As engineer: update a
threshold. Any user: password change, then list + revoke a session.

Demo script (record): 1 → 2 → 4 → 5 → 6 → 7 (3 optional). This is the version you
show investors.
