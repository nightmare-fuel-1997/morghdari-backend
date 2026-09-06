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
**Status:** complete (2026-09-06)

- SaloonTelemetryConsumer (apps/telemetry/ws_consumer.py) + ws.py service layer:
  JWT from `?token=` (executor-safe, never the ORM path), org-membership check,
  D21 `ws_max_connections_per_user` cap as a Redis SET + 300 s zombie-guard TTL
  refreshed on EVERY inbound message, auth-BEFORE-cap verdict order
  (4003 = auth/org, 4000 = full), register→re-check race guard.
- Tenant rule: saloons AND saloon_telemetry are FORCE-RLS'd, so every consumer
  query runs inside `transaction.atomic() + tenant_context(org_claim)` (the GUC
  is transaction-scoped — autocommit would kill it before the query; the WS twin
  of TenantOrgMiddleware). Explicit org comparison kept as the portable check —
  RLS is the backstop, never the only gate (test connections bypass RLS).
- config/asgi.py wrapped in ProtocolTypeRouter (http → Django, websocket →
  config/routing.py); ws.py `push_telemetry_updates` hooked into drain_stream
  AFTER the per-org commit — dashboards are only told about DURABLE rows.
- M6-07 gap-fill: `{"action":"gap_fill","since":...}` → ordered rows from the
  hypertable, optional slot_id filter, 200-row ceiling, friendly bad-since
  envelope; stateless merge contract (client walks the stamps).
- Test suite (17): two-browser group fan-out, group-scoping boundary,
  close-code matrix via close() spy, cap fill/free cycle, Set/TTL unit paths +
  Redis-down degradation, drain→push on the REAL pipeline (PG, recording layer),
  gap-fill matrix. House gotchas encoded in the module docstring: Channels runs
  the sync consumer in an EXECUTOR THREAD → socket-opening tests must be
  transaction=True; asgiref CANCELS the app task on a timed-out receive (use
  receive_nothing for silence probes); send_json_to is a coroutine (await it);
  PG refuses TRUNCATE on FK-referenced tables even when empty → the conftest
  cascade-flush patch is needed by plain transactional teardowns too.

## Phase 5 — Diff gate (M6-08)
**Status:** complete (2026-09-06)

- tests/test_openapi_contract.py verified green on PG: 3-test gate —
  (1) no undocumented extras (only the 4 ERRATUM_PENDING v0.5 lifecycle/
  rotation twins tolerated until their erratum), (2) KNOWN_GAPS monotonic
  shrink (stale entries fail like missing ones), (3) KNOWN_GAPS=∅ exit →
  the pair of asserts IS `implemented == contract`.
- Scope verified on the v1.5.0 yaml: exactly 34 paths (48 total − 4 platform
  v0.7 − 10 §4n v0.9; /api/compat/* absent from the yaml yet — M6-09 adds it
  as draft, still excluded). CI-active via docker-compose.ci.yml's pytest
  command (pytest --cov=apps --cov-fail-under=80).

## Phase 6 — Compat wave (M6-09, v1.6.0-draft)
**Status:** done (2026-09-06)

- ✅ `legacy_slug_map` (additive-only, unmanaged) + idempotent backfill; 15 REST
  routes under /api/compat/v1 + legacy aliases; flat-record translation;
  farm WS shim on the core saloon groups (cap + 4003/4000, never pushes
  buffered rows); `*_compat.py` modules only; excluded from the M6-08 gate.
- Verified: 18-test C1–C16 matrix green on PG/Timescale + Redis; sqlite quick
  loop 17+1skip (portable legacy_slug_map/saloon_telemetry DDL replays);
  full suite 402 passed @ 88.46%.

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
| M6-06: async connect denies `bad-token` while the identical sync probe passes | ~6 | Channels runs the sync consumer's connect() in an executor THREAD with its own DB connection — under TestCase's atomic block the uncommitted world rows are invisible there. Socket-opening WS tests must be `django_db(transaction=True)` |
| M6-06: consumer TS ran before GUC set → 4003 on every PG connect | 1 | `set_config(..., true)` is transaction-scoped; the consumer ran in autocommit. Wrap tenant_context + reads in one `transaction.atomic()` (the middleware's shape) |
| M6-09: DISTINCT ON crash on sqlite; `saloon_telemetry` absent from the quick loop entirely | 1 | DISTINCT ON → vendor branch (portable last-write-wins fallback); test.py's "create saloon_telemetry from state on sqlite" intent was an unimplemented comment — the compat module fixture now replays portable legacy_slug_map/saloon_telemetry DDLs so the full REST compat surface runs on the laptop loop |
| M6-09: PG password auth failed mid-session | 1 | The recreated dev postgres (wrong-compose container removed earlier) carries compose's real password `dev_password_only` — always take the URL from run_full.bat, not from memory |

## Next Step
Phase 7: M6-10 ops wave (v1.7.0-draft, Tracker §4p) — pagination convention,
O1–O12 ops read/update APIs (additive modules only), `user_sessions` table +
login/refresh/revoke writers, subscriber `diag:last:{slot}` diagnostics, then
the yaml ratification commit (v1.7.0 paths enter the contract → M6-08 gate
re-greened). Phases 2–6 are green: full gate 402 passed @ 88.46% on
PG/Timescale + Redis; contract gate still implemented == contract at 34 paths.
