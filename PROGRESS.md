# Progress — v0.5 Provisioning + Zero-Touch

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

## Next

- M5-10 owner steps: tag `v0.5`, record the WALKTHROUGH demo, KB/docs
  repo sync of the v0.5 release notes (backend_docs).
- v0.5 → v0.6 planning on owner go.