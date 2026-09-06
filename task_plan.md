# v0.6 Implementation Plan — Full REST + WebSocket (+ compat & ops extensions)

**Branch:** `v0.6-m6`
**Baseline:** v0.5 (commit `8b94218`, tag `v0.5`) — verified green 2026-09-05:
284 passed @ 89.24% + live-verify 10/10 on EMQX 5.5.
**Goal:** the complete live UX contract — all 32 core tenant endpoints, Channels
consumer, KPI + capability services, the `implemented == contract` gate on the
v1.5.0 yaml — plus the two additive scope extensions: Flutter compat adapter
(v1.6.0-draft, Tracker §4o) and ops read APIs (v1.7.0-draft, Tracker §4p).

## Phase 1 — Re-orient and scope
**Status:** complete

- Branch `v0.6-m6` created from main after the v0.5 close-out commit (`79a0838`).
- Task plan re-based: `versions/v0.6/TASKS.md` refreshed to contract v1.5.0
  (48 paths = 32 core + 12 §4n v0.9 + 4 platform) with M6-09 (compat) and
  M6-10 (ops) added; WALKTHROUGH extended with steps 6–7.
- Specs on hand: `docs/api-compat-flutter-v1.md`, `docs/api-ops-v1.md`,
  canonical yaml in backend_docs.

## Phase 2 — Core REST surface (M6-01..M6-03)
**Status:** complete (2026-09-06)

- v1.1 operability set, v1.3 KPI/dashboard set, v1.4 water/biosecurity/capabilities.
- Reuse v0.5 conventions: `require_role` choke point, RLS-scoped reads,
  `{"error": ...}` envelopes, config-driven caps.
- M6-03 delivered: water-intake (feed twin, audited upsert), biosecurity-events,
  capabilities (FEATURE_MATRIX service); 15-test suite; contract diff gate fully
  green (`KNOWN_GAPS` = ∅ → implemented == contract on the v1.5.0 v0.6 core).
  Full CI gate: 325 passed @ 88.71% on PG/Timescale + Redis.

## Phase 3 — Services (M6-04, M6-05)
**Status:** complete (2026-09-06)

- KPI computation service (D22 math, NULL-until-inputs) — services/kpi.py,
  (value, reason) tuples, water_feed_ratio, compliance band helpers; the
  /kpis/ view delegates to it. water:feed /kpis/ exposure → M6-10 ratification.
- Capability profile service (D23 FEATURE_MATRIX) — v1 set complete
  (fcr, water_ratio, thi, co, power, biosecurity_log, mortality_pct,
  compliance_pct); Requires.computed carries computed availability facts;
  unavailable→available demo green on PG (THI + compliance).

## Phase 4 — WebSocket (M6-06, M6-07)
**Status:** pending

- SaloonTelemetryConsumer: group push, ws:active cap (4000/4003), TTL refresh,
  reconnect gap-fill merge on `since`.

## Phase 5 — Diff gate (M6-08)
**Status:** pending

- CI `implemented == contract` on the 32-path v0.6 core of the v1.5.0 yaml
  (excludes platform (v0.7), §4n (v0.9), compat prefix, unratified v1.7 paths).

## Phase 6 — Compat wave (M6-09, v1.6.0-draft)
**Status:** pending

- `legacy_slug_map` + backfill; 15 REST routes + legacy aliases; flat-record
  translation; farm WS shim; `*_compat.py` modules only; excluded from the gate.

## Phase 7 — Ops wave (M6-10, v1.7.0-draft)
**Status:** pending

- Pagination convention; O1–O12; `user_sessions` + auth writers; subscriber
  `diag:last:{slot}`; ends with the yaml ratification commit → gate re-greened.

## Phase 8 — Integration and exit gate (M6-11)
**Status:** pending

- Full suite green (≥ 80% coverage gate), CHANGELOG entries, tag `v0.6`,
  WALKTHROUGH demo (steps 1–7), docs/KB sweep.

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| (carried from v0.5) stale `--reuse-db` test DB hides new migrations | 1 | Drop `test_morghdari_test` or `docker compose -p morghdari_ci down` before gate runs after schema-adding changes |

## Next Step
Phase 4: WebSocket (M6-06 SaloonTelemetryConsumer — group push
`saloon_{org_id}_{saloon_id}`, ws:active cap closing 4000/4003, TTL refresh;
then M6-07 reconnect gap-fill merge on `since`). Phases 2–3 are green:
full gate 338 passed @ 88.78% on PG/Timescale + Redis, diff gate at
implemented == contract.
