# Progress — v0.6 Full REST + WebSocket

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