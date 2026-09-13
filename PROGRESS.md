# Progress — v0.9-backend CLOSED (backend half) / v0.8-DR CLOSED / v0.7 Production Birth

## v0.9-backend CLOSED — 2026-09-12 (on `v0.9-pilot-backend`, tag `v0.9-backend`; field calendar parked, full `v0.9` open)

## v0.8-DR CLOSED — 2026-09-12 (on `v0.8-m8`, tag `v0.8-DR`; hardware tail parked as `v0.8-HIL`)

## v0.8 M8 start — DR + handover harness — 2026-09-12 (on `v0.8-m8`, owner go; hardware team procurement runs in parallel)

- **wal-g bake-in (M8-01):** `docker/postgres-walg/` image (timescale + wal-g v3.0.9, `archive_mode=on`, `archive_command`, `archive_timeout=300`); prod compose postgres → wal-g image + WALG/AWS env; rehearsal override builds locally + adds **MinIO** (S3-compatible target) so the T2 drill is executable with zero cloud S3 creds; `scripts/walg_base_backup.sh` for the weekly base.
- **T2 PITR drill (M8-02):** `scripts/pitr_rehearsal.sh` automates DR §3.1 end-to-end — parity baseline → WAL switch → target time → stop writers incl. EMQX → marker row → volume wipe → `wal-g backup-fetch` → `recovery_target_time` + `recovery.signal` → replay-pause wait → marker-absent/row-parity/hypertable checks (incl. v1.6.0 tables) → promote → RTO measured → DR-log row.
- **T3 Redis-wipe drill (M8-03):** `alerts.rebuild_active_state_from_db` **implemented** (Tracker §4l Q10 park lifted) — re-creates breach + active-alert keys from Postgres truth; LOS/REPLAY rows deliberately untouched; admin-or-default alias routing. `thresholds.recache_*` routed through the same alias — **fixes the M4-latent midnight-backstop no-op under FORCE-RLS**. `scripts/redis_wipe_drill.sh` runs the 5-leg drill (volume rm → rebuild → threshold warmer → LOS window → breach evidence → RTO/DR-log).
- **C5 + Hardware Handover Harness (M8-04-H):** `scripts/check_hil_readiness.py` (C1–C7 gaps: health, hardware/time, config route, O9, T5–T9 ACLs, OTA tooling, LOS tunable) + `scripts/ota_checksums.py` (T6 manifest). `scripts/hil_handover/hil_harness.py` = the receiver-side executable acceptance the ESP32 team passes at bring-up: P1 config fetch / P2 telemetry honesty / P3 LWT / P4 buffered replay (ORIGINAL timestamps) / P5 OTA / P6 rotation — `--target sim` (self-test) vs `--target real` (handover gate).
- **Tests/gates:** `test_m8_03_rebuild_drill.py` (5 tests: materialisation, LOS/replay skip, resolved-skip, idempotence, empty ledger). VALIDATED: py_compile green; unit module green on the sqlite quick loop; compose `config` parses; wal-g image build (docker build). **Live drill execution (RTO/RPO DR-log rows) = next step:** recompose the stack (new postgres image) then `REHEARSAL=1 scripts/pitr_rehearsal.sh` + `scripts/redis_wipe_drill.sh` — these take the rehearsal stack offline by design.
- **Status board:** `versions/v0.8/TASKS.md` — M8-01/02/03 LIVE-GREEN 2026-09-12 (T2 + T3 DR-log rows) + M8-04-H sim-green; M8-04..06 parked as `v0.8-HIL`; M8-07 DONE (CHANGELOG `[v0.8-DR]` + `docs/releases/v0.8-DR.md` + `versions/v0.8/DEMO-M8-DR.md`, tag `v0.8-DR`, push waived — no remote). CHANGELOG `[Planned → 0.8.0]` written. DR-log at `versions/v0.8/DR-LOG.md` (3 rows: T2 PASS, T3 run-1 VOID, T3 run-2 PASS).

## Contract v1.7.2 — follow-through CORE (F1–F15 + member role change) — 2026-09-12 (code + gates GREEN; sealed by M0 close-out)

- **Why:** an audit of the shipped API ("does every subject have CRUD? is every
  engine dependency writable?") found four writers the ENGINE needed and no API
  could produce: `flock_cycles.end_date` (closing a cycle was impossible while the
  KPI windows, the quality verdicts and the threshold hash all key off it),
  `mortality_records` (the KPI's `mortality_pct` could only ever read zero),
  `maintenance_mutes` (read by every alert evaluation, writable only from the ORM
  — the model claimed "Django Admin" and no registration ever shipped), and the
  tenant topology itself (farm/saloon/position existed only via SQL). Plus: a
  member's role could not be changed, and the `reviewer` role the v1.5 reports
  wave defined was ungrantable (the invite enum rejected it).
- **Contract:** `backend/openapi.yaml` 1.7.1 → **1.7.2** (+20 paths: F1–F10 and
  F14/F15, the mute trio F11–F13, `PATCH /org/members/{id}/`, and the four
  manager-side gateway twins whose **erratum is now ratified** — the contract
  gate's `ERRATUM_PENDING` set is EMPTY and pinned by a new test).
  `SPECTACULAR_SETTINGS` version/description follow it; `openapi.generated.yaml`
  regenerated.
- **Code:** `apps/farms/api_follow.py` + `urls_follow.py`,
  `apps/alerts/api_follow.py` + `urls_follow.py`,
  `apps/accounts/views.py::update_member_role`, guards in
  `apps/farms/services_follow.py` + `apps/alerts/services_follow.py`, slug
  convention extracted to `apps/compat/legacy_slugs.py` (the backfill command now
  calls it). Additive-only: **zero new tables, zero migrations**
  (`makemigrations --check` → "No changes detected").
- **Traps handled:** (1) Django resolves the first matching pattern regardless of
  method, so `POST /farms/` and `PATCH|DELETE /saloons/{id}/` EXTEND the existing
  views — a second pattern would 405; (2) a farm/saloon created through the API
  must mint its `legacy_slug_map` row in the same transaction, or the Flutter
  aliases 404 it until somebody re-runs the backfill; (3) Django's delete
  collector walks the UNMANAGED history tables, so hard deletes are judged by a
  shared guard that refuses when the backend cannot verify dependents; (4) a
  refused delete must not leave a `*_DELETED` audit row (audit is written after
  the delete succeeds); (5) **found by Postgres, invisible on sqlite:**
  `flock_cycles.saloon` is `ON DELETE RESTRICT`, so F6's "no active flock" rule
  is unachievable — a CLOSED cycle binds the saloon forever (Django's collector
  only emulates that constraint on sqlite, so the local loop passed while the
  guard was reaching a row the DB would never release). The guard now reports
  the schema's rule: `saloon has an active flock` / `saloon has flock history` /
  `saloon has telemetry history`, and the contract says the same thing.
- **Tests:** `backend/tests/test_followthrough_core.py` — 16 tests; on the sqlite
  loop 14 pass / 2 skip (both Postgres-only: the compat-slug resolver and the new
  telemetry-history delete verdict), and **16 pass on the CI Postgres/Timescale
  stack**. The module also pins the "no audit row on a refused delete" rule and
  the KPI number moving after an API-created mortality row.
- **Gates (2026-09-12):** contract gate GREEN (5 tests), docs gates GREEN
  (`test_openapi_schema.py` + `test_docs.py`), `makemigrations --check` →
  "No changes detected", `openapi.generated.yaml` regenerated (mirror route set
  matches the code), new-file coverage 89–100%, and the **full suite on
  Postgres/Timescale + Redis: 568 passed, 3 skipped** — which is where the RLS
  proofs, the compat-slug resolver and all three PG-only delete branches ran.
- **Still to run:** the module-coverage script and the migrate-gate sweep against
  a real CI image rebuild (Docker Hub/network timed out, so the suite ran on the
  prebuilt `morghdari-test:ci` image with the working tree mounted at `/app`).
  Known local-only noise: `test_m6_10_ops` O9/O10 fixtures need the broker, so
  that module fails on a laptop without Redis and passes in the container.
- **Tree state:** the wave is UNCOMMITTED (no commit was requested); the parked
  sibling-writer files (`monitoring/0001`, `tenants/0002`, `reports/0001`) were
  left untouched, as was the untracked staff-side draft
  `docs/api-topology-write-v1.md` (another thread's work, whose §8 asks this
  wave's guards be imported rather than duplicated — they are import-ready).

## Migrate-chain reconciliation — 2026-09-12 (branch tip is self-sufficient again)

Three files had been parked uncommitted since the v0.7 tail, which meant a clean
checkout of the tip could not pass the migrate gate on its own. Both real failures were
reproduced before fixing, and both were invisible to the gates that had been running:

- **`monitoring/0001` — state registration.** `OrgDailyUsage` is `managed=False`, and its
  migration carried only the RunSQL that creates it. From a `git archive HEAD` tree,
  `makemigrations --check` exited 1 asking for a phantom `monitoring/0002_initial.py`
  ("+ Create model OrgDailyUsage"). Landing the parked `CreateModel` state block (the
  compat/0001 pattern) turns it CLEAN. No DDL changes — unmanaged — so already-migrated
  databases are unaffected.
- **`tenants/0002` — dependency pinned one migration too early.** The policy universe
  includes `alert_acknowledgements`, whose `org_id` column only appears in `alerts/0004`.
  On a fresh Postgres, a scoped `python manage.py migrate reports` died with
  `column "org_id" does not exist` inside the `CREATE POLICY tenant_isolation ON
  alert_acknowledgements` statement; with the dependency bumped to `alerts/0004` the same
  command completes. The full `migrate` never hit it — whole-graph ordering happens to
  place `alerts/0004` first — so this was a scoped-deploy/backfill-only landmine that no
  gate could see.
- **`reports/0001` — dropped, not committed.** The parked edit added four unused imports
  (`deletion`, `settings`, `Q`/`F`) plus a duplicate of the two imports already below them,
  with no functional content (HEAD's file already carries its four `CreateModel` state
  registrations). Restored to HEAD so the commit carries no dead code; the exact diff is
  recorded in the session log if it turns out to have been intentional.

**Stale-image trap (found here, worth remembering):** the first `migrate-gate` run in this
session reported CLEAN / "69 migrations recorded" / DEPLOYABLE — a truthful report about a
16-hour-old tree. `docker compose ... run --rm migrate-gate` without `--build` reused the
`morghdari_ci-migrate-gate:latest` image cached by the previous session (2026-09-11 01:08),
which predates the v1.7.1 migrations; none of `alerts.0005`/`farms.0002`/`tenants.0005` were
applied. The canonical `scripts/run_acceptance.sh` hardcodes `--build` (DEPLOYMENT §2 warns
about exactly this), so the ritual was never wrong — a hand-typed gate command was.

**Verification (this tree, canonical flags):** `migrate-gate` with `--build` → **CLEAN /
73 migrations recorded / DEPLOYABLE**, all three v1.7.1 migrations applied and every app
unwinding to zero; full test gate with `--build` → **551 passed, 3 skipped**, module
coverage gate GREEN; fresh `git archive HEAD` tree → `makemigrations --check` CLEAN and
scoped `migrate reports` green.

## Contract v1.7.1 — threshold-template write surface (O13–O16) — 2026-09-12 (in flight)

- **Contract**: `backend/openapi.yaml` 1.7.0 → **1.7.1** — +4 paths (`POST /threshold-templates/`,
  `PUT /threshold-templates/{id}/`, `POST /threshold-templates/{id}/publish/`,
  `GET`+`PUT /saloons/{id}/threshold-template/`) and 3 components (`ThresholdTemplate`,
  `ThresholdTemplateDetail`, `ThresholdTemplateRule`, `ThresholdTemplateRuleInput`,
  `SaloonTemplateAssignment`); `SPECTACULAR_SETTINGS` version/description follow it and
  `backend/openapi.generated.yaml` was regenerated with the README command.
- **Gates (local, sqlite, 2026-09-12)**: contract gate GREEN — `test_openapi_contract.py`
  4 passed; docs gates GREEN — `test_openapi_schema.py` + `test_docs.py` alongside it
  (12 passed); `test_m6_11_template_writes.py` **51 passed in ~3 s** (autouse cache stub,
  so the module genuinely needs no Redis); `tests/test_m6_10_ops.py -k TestO4Templates`
  2 passed; `manage.py makemigrations --check` → "No changes detected".
- **Full CI gate GREEN (2026-09-12, Postgres/Timescale + Redis)**: **551 passed, 3 skipped
  @ 89.92%**, exit 0 (`docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm
  test`, image rebuilt, `down -v` first so the reused test DB could not hide a
  column-adding migration); module coverage gate GREEN.
- **Migrate gate GREEN (re-run with `--build`, see the reconciliation section above — the
  first run had silently used a stale image)**: RESULT: DEPLOYABLE — makemigrations --check
  CLEAN, forward + idempotent re-run, **73 migrations recorded**, every app unwinds to zero
  (the 3 new migrations — alerts 0005, farms 0002, tenants 0005 — apply and reverse cleanly).
- **Not covered by a gate**: `scripts/check_rls_parity.py` reports pre-existing drift
  (initdb/02_rls.sql + tenants/0002 list 36 tables; `database.md` has 47 org-scoped ones —
  the §4n reports/panels/subscriptions tables from the v0.6–v0.7 waves were never added to
  either ARRAY). It is NOT wired into CI and this wave did not change it (the diff there is
  the SELECT-only built-in-catalog policy, not a table). Worth a small follow-up gate.
- **Tree state**: the wave is UNCOMMITTED (code + tests + contract + docs); the parked
  sibling-writer files (`monitoring/0001`, `tenants/0002`, `reports/0001`) were left
  untouched, as was the untracked `docs/api-topology-write-v1.md` (a v0.8/v1.10.0 topology
  spec that is not part of this wave).

## M7-10 DoD sweep — 2026-09-10 (v0.7 CLOSED)

- **CHANGELOG**: `[v0.7]` section written (risk killed / Added M7-01..M7-17 / live-drill fixes / verification / deployment notes); release drafts re-pointed at shipped state (`v1.6-compat-draft` scope-reduced, `v1.7-ops-draft` ratified, `api-reports-merge-v1` delivered, `api-compat-flutter-v1` scope-noted).
- **Release note**: `docs/releases/v0.7.md` (house v0.5 format — scope, condensed changelog, acceptance table, verification, next steps).
- **Tag `v0.7`**: annotated on the last functional commit (the M7-10 live-drill fix), following the v0.5/v0.6 tag style (push waived — no remote).
- **Live drills**: 10/10 legs green over real HTTPS/TLS on the rehearsal stack; transcript `versions/v0.7/DEMO-M7.md` — recording waived by owner (v0.5 precedent), runnable on request.
- **Docs/boards sweep**: repo-local only per owner (backend_docs + KB resync stay parked); `versions/v0.7/TASKS.md` M7-10/14/16/17 ticked, `task_plan.md` Phases 4–8 flipped, Next Step → v0.8 planning on owner go.

## Verification — v0.7 (final, 2026-09-10)

- **Full CI gate**: 513 passed, 3 skipped @ 88.09% coverage (Postgres/Timescale + Redis), exit 0; module coverage gate GREEN; contract gate green on the ratified v1.7.0 yaml (`implemented == contract`, `KNOWN_GAPS` empty).
- **Migrate gate**: RESULT: DEPLOYABLE — 69 migrations forward + idempotent re-run on a throwaway DB, every app unwinds to zero.
- Milestones M7-01..M7-17 + M7-10 all ✅ in `versions/v0.7/TASKS.md`.

## M6-11 DoD sweep — 2026-09-06 (v0.6 CLOSED)

- **CHANGELOG**: `[v0.6]` section written (risk killed / Added M6-01..08 /
  compat + ops / verification / deployment notes) with
  `[Unreleased → 1.6.0]` + `[Planned → 1.7.0]` stubs noting both waves
  shipped inside v0.6.
- **Release note**: `docs/releases/v0.6.md` (house v0.5 format — scope,
  condensed changelog, acceptance table, verification, next steps).
- **Tag `v0.6`**: annotated on the final M6-10 commit (`1e387a4`),
  following the v0.5 tag style.
- **WALKTHROUGH**: `versions/v0.6/WALKTHROUGH.md` carries the full 1–7
  demo script (diff-gate, two-window live, gap-fill, KPIs, capabilities,
  Flutter compat, ops reads); **recording waived by owner** (v0.5
  precedent) — the script stays runnable on request.
- **Docs/KB sweep**: ops spec erratum already written
  (`docs/api-ops-v1.md` — O2/O3 bent to the real day-ranged min/max
  schema + §7 storage shapes); release drafts (`v1.6-compat-draft.md`,
  `v1.7-ops-draft.md`) already carry the shipped-state status.

## Verification — v0.6 (final, 2026-09-06)

- **Full CI gate**: 436 passed @ 88.11% coverage (Postgres/Timescale +
  Redis), exit 0; contract gate green on the ratified v1.7.0 yaml
  (`implemented == contract`).
- Milestones M6-01..M6-11 all ✅ in `versions/v0.6/TASKS.md`.

## v0.5 archive

## Full verification — 2026-09-05 (BOTH GATES GREEN)

- **CI gate**: `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm
  --build test` → **284 passed, 3 skipped, 89.24% coverage** (fresh build,
  TimescaleDB 2.29.2-pg16 + redis:7-alpine).
- **Live broker verification**: `run --rm live-verify` → **10/10 PASS**
  against real EMQX 5.5 (plaintext authn-user creation, wrong-password
  CONNECT rejection, allow-rule publish delivered, revoke-to-empty drops
  delivery, rotation commit: new password connects, old rejected).
- **Pitfall found & fixed (stale `--reuse-db`)**: the Aug-24 `morghdari_ci`
  postgres container was only STARTED (not recreated), so its anonymous
  volume held a v0.4-era `test_morghdari_test`. Under
  `--nomigrations --reuse-db`, `migrate --run-syncdb` creates MISSING
  tables but never ALTERs existing ones → v0.5's `accounts.0002`
  (`staff_role`) was absent → 103 failed / 39 errors on the first run.
  Fix: `DROP DATABASE test_morghdari_test` and re-run (fresh DB from
  current models). CI project torn down afterwards so every future gate
  run starts fresh; same trap will bite again if a CI postgres container
  survives a schema-adding change — drop the test DB or `down` first.

## Validation — 2026-09-02 (SUITE GREEN)

Full backend suite against the Docker CI stack
(timescale/timescaledb:2.29.2-pg16 + redis:7-alpine):

- **284 passed, 3 skipped, 89.24% coverage** — gate (80%) met, fresh
  test DB, no background test container racing the runner.
- All M5 feature work implemented and tested: trust boundary
  (M5-04), FM lifecycle (M5-05), gateway config (M5-06), EMQX ACL
  wiring + authn-user sync (M5-07), staged rotation with broker push
  (M5-08), concurrency + state-matrix guards (M5-09). See
  `versions/v0.5/TASKS.md` for per-task status.
- **Live broker verification** (`scripts/live_verify_emqx.py`, EMQX
  5.5.1, 10/10 green): plaintext user creation (the broker contract),
  wrong-password CONNECT rejection, allow-rule publish delivered,
  revoke-to-empty drops delivery (no_match=deny), rotation with new
  password accepted and old rejected — the M5-07/M5-08 acceptances CI
  can only mock are now proven live.
- Demo dashboard validated against PostgreSQL: identity, sensor
  health, rolling telemetry statistics, per-slot stats, EMQX status,
  simulate + farm topology; Redis-backed online/offline behavior
  confirmed (pack LWT keys via the status-topic subscription).

## Environment

- Docker PostgreSQL/TimescaleDB:
  `postgres://morghdari_dev:dev_password_only@localhost:5432/morghdari_dev`
  (dev, host) / `postgres://morghdari_test:ci_password_only@postgres:5432/morghdari_test`
  (CI compose `redis://redis:6379` without a password).
- CI commands: `docker compose -f docker-compose.ci.yml run --rm test`
  (pytest gate; image must be rebuilt after source edits:
  `docker compose build test`) and
  `docker compose -f docker-compose.ci.yml run --rm live-verify`
  (real-EMQX M5-07/08 gate — `scripts/live_verify_emqx.py` against the
  compose `emqx` service, so broker semantics are never only mocked).

## Issues fixed along the way

- Subscriber boot: broker-unreachable retry every 2 s + status-topic
  subscription + correct status-subscription logging (boot tests green).
- Test Redis URLs derived from the env URL instead of hardcoded
  `localhost` strings (host runs and Docker CI both work); CI Redis URL
  `/0` suffix removed.
- Fresh CI volumes: `morghdari_app` table grants are now applied at
  session setup (explicit GRANT on ALL tables) — a brand-new test DB
  previously had no grants because `ALTER DEFAULT PRIVILEGES` only
  covers future tables; RLS/tenant suites now pass from zero.
- Request-approval credentials hashed with EMQX-native `$2b$` bcrypt
  (C7) instead of Django `bcrypt_sha256` wrappers EMQX cannot verify.
- Temporary SQLite compatibility reviewed: the only SQLite-specific
  paths are the tenant-middleware GUC bypass and `DEBUG=True` in
  `config/settings/test.py` (enables the gated demo routes in tests).
  Both are scoped to SQLite/test and never weaken the PostgreSQL/RLS
  production path (CI runs PostgreSQL exclusively).

## Next — M5-10 (2026-09-05, CLOSED by owner decision)

- ✅ Tag `v0.5` created locally on `8b94218` (annotated, verified via
  `git cat-file -t v0.5` → `tag`).
- ✅ Push waived (owner 2026-09-05) — tag stays local.
- ✅ WALKTHROUGH demo recording waived for now (owner 2026-09-05) —
  the full 6-step drill remains in `versions/v0.5/WALKTHROUGH.md`
  and can be run/recorded later on owner request.
- Docs-side v0.5 release notes already in backend_docs
  (`docs/releases/v0.5.md`).
- Next: v0.6 planning on owner go (spiral gate satisfied by owner waiver).