# v0.6 TASKS — ordered breakdown

_Status (2026-09-05): PLANNING REFRESH on branch `v0.6-m6` — scope re-based from
contract v1.4.2 to the canonical **v1.5.0** yaml (48 `/api/v1` paths: 32 v0.6 core +
10 §4n paths + 2 org-member paths built at v0.2 + 4 platform) and extended with the two additive
v0.6 scope extensions: **v1.6.0-draft** Flutter compat (Tracker §4o) and
**v1.7.0-draft** ops reads (Tracker §4p). Specs: `docs/api-compat-flutter-v1.md`,
`docs/api-ops-v1.md`. Baseline: v0.5 green (284 passed @ 89.24% + live-verify 10/10)._

| # | Task | Depends on | Acceptance | Status |
|---|---|---|---|---|
| M6-01 | v1.1 operability endpoint set complete — **15 NEW endpoints** (saloons detail/alerts/analytics-hourly, farms overview, provisioning reads: location-tree/provinces/cities/villages/positions, ack acknowledge, operational-state, flocks incl. `placement_count` body, metric-types, health extended, hardware/time) over the 8 paths already built in v0.2/v0.3/v0.5 (auth ×2, telemetry, sync/approve/reject, gateway-requests+review, config) | v0.5 | OpenAPI diff green for the set | ✅ (2026-09-06: 15 endpoints implemented + gate green; full suite on Postgres/Timescale + Redis: 328 passed @ 87.85%) |
| M6-02 | v1.3 KPI/dashboard endpoints (**5**: weight-samples, feed-intake POST/GET, kpis, me/dashboard-config, dashboard-config/{role}) | M6-01 | diff green; D22 tests green | ⬜ |
| M6-03 | v1.4 water/biosecurity/capabilities endpoints (**3**: water-intake POST/GET, biosecurity-events POST/GET, capabilities) | M6-01 | diff green; capability gating tests green | ⬜ |
| M6-04 | KPI computation service (D22 math incl. water:feed ratio) | M6-02 | §2.7 suites green | ⬜ |
| M6-05 | Capability profile service (FEATURE_MATRIX from D23) | M6-03 | unavailable→available demo works | ⬜ |
| M6-06 | Channels consumer: group push (`saloon_{org_id}_{saloon_id}`), ws:active cap, close codes, TTL refresh | v0.3 | two browsers see same update; cap closes **4000** (4003 = auth/org) | ⬜ |
| M6-07 | Reconnect gap-fill merge behavior (client `since`) | M6-06 | gap-fill test green | ⬜ |
| M6-08 | OpenAPI diff test in CI (`implemented == contract`, **v1.5.0 yaml**; gate scoped to the non-platform, non-§4n paths — **34 = the canonical 32 + 2 org-member paths built at v0.2**; excludes the 4 `/platform/support/*` paths (v0.7), the 10 §4n paths (v0.9), the `/api/compat/*` prefix (M6-09) and the v1.7 ops paths until their ratification commit in M6-10) | M6-01..03 | CI gate active | ⬜ |
| M6-09 | **NEW (v1.6.0-draft, Tracker §4o):** Flutter compat adapter — additive-only: `legacy_slug_map` table + idempotent backfill, 15 REST routes under `/api/compat/v1` + legacy `/api/...` aliases, flat telemetry translation (exact Flutter keys, `{"farm_history": [...]}` envelope), farm WS shim (`consumers_compat.py` fanning out to the saloon groups, close 4003/4000), new `*_compat.py` modules only, zero ALTER on core tables, zero change to the 48 `/api/v1` paths, prefix excluded from the M6-08 gate | M6-08 (gate green first so compat never masks core drift) | C1–C16 matrix vs the shipped Flutter calls; foreign-slug → 404; `ws:active` cap honored on the shim | ⬜ |
| M6-10 | **NEW (v1.7.0-draft, Tracker §4p):** ops read APIs — pagination convention (`limit`/`offset` → `{results, count}`), O1–O12 (audit-log query, threshold read/update + templates, saloon photos, gateway reads + `diag:last:{slot}` diagnostics, password change with `auth_strict` throttle, `user_sessions` table + login/refresh writers + revoke) — additive modules only; ends with the yaml ratification commit (v1.7.0 paths enter the contract → M6-08 gate re-greened) | M6-08 (parallel with M6-09) | O1–O12 role matrix green; foreign-id → identical 404; ratification diff test green | ⬜ |
| M6-11 | DoD sweep: CHANGELOG ([v0.6] + [Unreleased → 1.6.0] + [Planned → 1.7.0]), tag `v0.6`, demo recorded (WALKTHROUGH full sequence), docs/KB swept | all | tag exists | ⬜ |

## Notes

- The compat (M6-09) and ops (M6-10) waves are **additive scope extensions recorded in
  VERSIONS.md §v0.6 / Tracker §4o+§4p** — no new spiral, no reorder; they ride this version.
- Contract edits to the yaml happen ONLY in the M6-10 ratification commit (docs-repo rule).
- Full suite must stay ≥ 80% coverage gate; compat/ops modules ship with their own suites.
