# v0.7 Implementation Plan — Real VPS + Observability (+ native v1.6, reports-merge, support)

**Branch:** `v0.7-m7` (cut from `docs/sync-4s` @ `0271574`)
**Baseline:** v0.6 close (commit `2efd17f`, tag `v0.6` on `1e387a4`) — 436 passed @
88.11% (Postgres/Timescale + Redis); contract gate implemented == contract on the
ratified v1.7.0 yaml (59 paths).
**Goal:** the system runs on real hardware and screams when it's sick — plus the
owner's 2026-09-07 scope decisions (Tracker §4s): the contract union stays **v1.7.0**
(compat routes + reports-merge W-aliases absorb at the v0.7 ratification; 1.8.0 stays
reserved for growth), and the **reports-merge implementation moves into v0.7**
(OPEN-4s deadline: end of v0.7) — the §4n reports base it rides on comes forward with it.

## Phase 1 — Re-orient and scope (M7 planning refresh)
**Status:** complete (2026-09-07)

- Branch `v0.7-m7` created from `docs/sync-4s` (which already carried the S4s docs
  sync + the M7-11..14 task additions on top of the v0.6 close).
- `versions/v0.7/TASKS.md` re-based: baseline header, owner decisions recorded,
  reports-merge added as **M7-15..M7-17** (base reports surface → merge surface +
  `daily_report_extras` → compat W1–W5 aliases + ratification).
- Split established: **code-side waves** (M7-07, M7-11..M7-12, M7-15..M7-17) run on
  the laptop; **VPS-resident waves** (M7-01..M7-06, M7-08, M7-09, M7-13) are
  owner-gated on VPS access; **M7-14** (compat removal) is hard-sequenced behind the
  M7-13 staging proof.

## Phase 2 — M7-11 native v1.6 lists (GET /api/v1/farms/, GET /api/v1/saloons/)
**Status:** in progress

- `apps/farms/api.py`: `farm_overview` body extracted into a payload helper reused
  by the list (identical response shapes — M6-01 tests must stay byte-green);
  `farm_list` returns `FarmOverview[]` (`?org_id=` optional, foreign org → 403);
  `saloon_list` returns `SaloonDetail[]` (`?farm_id=` filter, foreign farm → 403).
- Role-scope narrowing via `scoped_farms`/`scoped_saloons` (M2-06 choke point) on
  top of the explicit org filter — RLS stays the Postgres backstop.
- `openapi.yaml` +2 paths (`/farms/`, `/saloons/`); contract-gate root list already
  includes `apps.farms.urls` → gate re-greens once paths + routes land together.

## Phase 3 — M7-12 farm telemetry + paging
**Status:** pending

- `GET /api/v1/farms/{farm_id}/telemetry/` — TelemetryReading shape merged across
  the farm's saloons (compat `telemetry_farm_history` is the aggregation precedent:
  `filter(org_id, saloon__farm_id=...)`), rows gain `saloon_id`; acceptance: farm
  aggregate == union of saloon rows for the same window.
- `?limit=`/`?offset=` on `GET /saloons/{saloon_id}/telemetry/` — paging mode is
  newest-first so `?limit=1` IS the latest reading (the Flutter chart calls
  `?limit=24`); no-param behavior stays byte-identical (chronological ascending).
- `openapi.yaml` +1 path; gate green.

## Phase 4 — Reports-merge wave (M7-15 → M7-16 → M7-17)
**Status:** pending

- M7-15 §4n base: `daily_reports` + `report_medication_usages` (database.md
  verbatim, additive migrations, FORCE-RLS template, org_id denormalized), the v1.5
  endpoints (list/POST/PATCH), state machine DRAFT→SUBMITTED→APPROVED/REJECTED +
  FLAGGED branch (medications → auto medical ticket + WS push to doctor), Tehran-day
  edit time-lock (D24-3) with System Admin/Support exemption audited.
- M7-16 merge: `daily_report_extras` 1:1 side table (zero ALTER), alias
  normalization (casualties_today→mortality_count, is_draft→status, Persian
  literals), new fields + checklist enforcement (SUBMITTED with incomplete checklist
  → 400), single-report GET `/{report_date}/`, `is_completed` +
  `reported_by`/`reported_at` response aliases, old-payload byte-compat.
- M7-17 compat aliases W1–W5 (C-09 superseded for reports only) + the v0.7
  ratification commit (openapi.yaml absorbs compat routes + W-aliases into v1.7.0;
  gate re-scoped and re-green).

## Phase 5 — M7-07 support activation (code side)
**Status:** pending

- Staff MFA enforced (django-otp TOTP) on platform-staff accounts; the 4 v1.2
  support-op endpoints (`/platform/support/*` — session, gateway suspend via
  provisioning, alert force-resolve, task drain) live behind the audited
  org-context session; superuser path untouched.

## Phase 6 — VPS-resident waves (owner-gated)
**Status:** blocked on VPS access

- M7-01..M7-06 (provision, TLS, prod compose, monitoring, backups, subscriber
  rebalance), M7-08 (prober + cost metering), M7-09 (chaos drill), M7-13 (frontend
  migration proof on staging). Each needs the real VPS; runbooks/compose artifacts
  land with their tasks once access exists.

## Phase 7 — M7-14 compat removal (hard-sequenced)
**Status:** pending (after the M7-13 staging proof)

- Unmount urls_compat → delete apps/compat + tests → migration dropping
  legacy_slug_map. Gate is prefix-neutral (compat was never in the root list) —
  verify anyway.

## Phase 8 — M7-10 DoD sweep
**Status:** pending

- CHANGELOG `[v0.7]`, release note `docs/releases/v0.7.md`, tag `v0.7`, docs/KB
  sweep, demo script per WALKTHROUGH.

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|

## Next Step
M7-11 implementation (Phase 2), then M7-12, then the reports-merge wave.
