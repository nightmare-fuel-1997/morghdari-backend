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
**Status:** ✅ complete (2026-09-07)

- `apps/farms/api.py`: `farm_overview` body extracted into a payload helper reused
  by the list (identical response shapes — M6-01 tests must stay byte-green);
  `farm_list` returns `FarmOverview[]` (`?org_id=` optional, foreign org → 403);
  `saloon_list` returns `SaloonDetail[]` (`?farm_id=` filter, foreign farm → 403).
- Role-scope narrowing via `scoped_farms`/`scoped_saloons` (M2-06 choke point) on
  top of the explicit org filter — RLS stays the Postgres backstop.
- `openapi.yaml` +2 paths (`/farms/`, `/saloons/`); contract-gate root list already
  includes `apps.farms.urls` → gate re-greens once paths + routes land together.

## Phase 3 — M7-12 farm telemetry + paging
**Status:** ✅ complete (2026-09-07)

- `GET /api/v1/farms/{farm_id}/telemetry/` — TelemetryReading shape merged across
  the farm's saloons (compat `telemetry_farm_history` is the aggregation precedent:
  `filter(org_id, saloon__farm_id=...)`), rows gain `saloon_id`; acceptance: farm
  aggregate == union of saloon rows for the same window.
- `?limit=`/`?offset=` on `GET /saloons/{saloon_id}/telemetry/` — paging mode is
  newest-first so `?limit=1` IS the latest reading (the Flutter chart calls
  `?limit=24`); no-param behavior stays byte-identical (chronological ascending).
- `openapi.yaml` +1 path; gate green.

## Phase 4 — Reports-merge wave (M7-15 → M7-16 → M7-17)
**Status:** ✅ done (2026-09-09; proven live 2026-09-10)

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
**Status:** ✅ done (2026-09-09; proven live 2026-09-10 — MFA 7/7, session opens after the tenant_context fix, suspend→PUB-denied→resume→flows, force-resolve idempotent, drain 202)

- Staff MFA enforced (django-otp TOTP) on platform-staff accounts; the 4 v1.2
  support-op endpoints (`/platform/support/*` — session, gateway suspend via
  provisioning, alert force-resolve, task drain) live behind the audited
  org-context session; superuser path untouched.

## Phase 6 — VPS-resident waves (owner-gated)
**Status:** ✅ done as laptop-as-VPS rehearsals (owner decision 2026-09-08; VPS-day runbook steps remain in scripts/)

- M7-01..M7-06 (provision, TLS, prod compose, monitoring, backups, subscriber
  rebalance), M7-08 (prober + cost metering), M7-09 (chaos drill), M7-13 (frontend
  migration proof on staging). Each needs the real VPS; runbooks/compose artifacts
  land with their tasks once access exists.

## Phase 7 — M7-14 compat removal (hard-sequenced)
**Status:** ✅ done (2026-09-09: SCOPED per owner — C1–C15 deleted in `34e0d08`, W1–W5 + C16 + slugs retained; proven live 2026-09-10 — removed routes 404, W-routes live)

- Unmount urls_compat → delete apps/compat + tests → migration dropping
  legacy_slug_map. Gate is prefix-neutral (compat was never in the root list) —
  verify anyway.

## Phase 8 — M7-10 DoD sweep
**Status:** ✅ done (2026-09-10)

- CHANGELOG `[v0.7]`, release note `docs/releases/v0.7.md`, tag `v0.7`, repo-local docs sweep
  (shipped-state draft statuses), live drills 10/10 + `versions/v0.7/DEMO-M7.md` transcript
  (recording waived per v0.5 precedent). **v0.7 is closed.**

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|

## Post-v0.7 addendum — contract v1.7.1 threshold-template writes (2026-09-12)
**Status:** code + contract done, full gates pending.

- The owner lifted the seeded-only template rule (O13–O16): drafts by engineer+ inside
  granted farms (fail-closed), publish/assign by owner/manager, only PUBLISHED rows arm
  alarms, and resolution now starts with the saloon's explicit assignment.
- `openapi.yaml` ratified 1.7.0 → 1.7.1 (+4 paths, 3 new components); mirror regenerated.
  Local: contract + docs gates GREEN, `test_m6_11_template_writes.py` 51 passed,
  `makemigrations --check` clean.
- Open: the CI gate (Postgres/Timescale + Redis), module coverage gate and migrate-gate
  for the 3 new migrations (Docker down at sweep time) — see `PROGRESS.md`.

## Post-v0.7 addendum — contract v1.7.2 follow-through CORE (F1–F15) (2026-09-12)
**Status:** DONE — code + contract + tests; full suite GREEN on Postgres/Timescale
+ Redis (568 passed, 3 skipped). Sealed by the M0 close-out commit.

- An audit of the shipped surface (does every subject have CRUD? is every engine
  dependency writable?) found the gaps this wave closes: no writer for
  `flock_cycles.end_date` or `mortality_records` (while the KPI read the latter),
  no surface at all for `maintenance_mutes` (read by the alert engine, writable
  only from the ORM — the model said "Django Admin" and no registration shipped),
  no tenant topology writes (farm/saloon/position came only from SQL), no way to
  change a member's role, and the `reviewer` role the v1.5 reports wave defined
  was ungrantable.
- Implemented the documented follow-through CORE (`docs/api-followthrough-v1.md`
  F1–F15) + `PATCH /org/members/{member_id}/`: topology CRUD with guarded deletes,
  flock close, direct mortality entry, the org alert feed, the home rollup and the
  mute windows. **Zero new tables, zero migrations** — the absorbed half of the
  spec touches frozen tables only.
- Contract ratified 1.7.1 → **1.7.2** (+20 paths: 16 new + the 4 manager-side
  gateway twins whose erratum was still pending); mirror regenerated; contract gate
  re-green with `ERRATUM_PENDING` now EMPTY.
- Traps found and fixed on the way: Django resolves the FIRST matching pattern
  regardless of method (so `POST /farms/` and `PATCH|DELETE /saloons/{id}/` extend
  the existing views instead of shadowing them); a new farm/saloon must mint its
  `legacy_slug_map` row in the same transaction or the Flutter aliases 404 it; a
  refused delete must not leave a `*_DELETED` audit row; and — the one only
  Postgres could show — `flock_cycles.saloon` is `ON DELETE RESTRICT`, so F6's
  "no active flock" delete rule was unreachable (a closed cycle binds the saloon
  forever), so the endpoint now reports the schema's real rule.
- Open: the module-coverage script and the migrate-gate sweep against a rebuilt CI
  image (Docker Hub unreachable; the suite ran on the prebuilt `morghdari-test:ci`
  image with the tree mounted) — see `PROGRESS.md`.

## Next Step
v0.8 planning on owner go. v0.7 is CLOSED: all M7-01..M7-17 + M7-10 ✅, full gate
513 passed, 3 skipped @ 88.09% on PG/Timescale + Redis, contract gate implemented ==
contract at the ratified v1.7.0 yaml, migrate-gate RESULT: DEPLOYABLE (69 migrations),
live drills 10/10 legs green, tag `v0.7` in place (push waived — no remote).
