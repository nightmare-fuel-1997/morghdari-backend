# PROGRESS — v0.1 Walking Skeleton + Schema/RLS

> EN: Live mirror of `versions/v0.1/TASKS.md`. A task is done when its ACCEPTANCE
> passes (not when code "looks right"). Each line links to the commit that closed it.
>
> فارسی: فهرست زندهٔ وظایف نسخهٔ v0.1 — هر وظیفه فقط با قبولیِ «پذیرش» بسته می‌شود.

## M0 — walking skeleton

- [x] M0-01 git branch `v0.1-m0` created; `backend/` folder exists — `1748510`
- [x] M0-02 requirements per Blueprint §2 pins (`pip list` shows psycopg 3.x) — `9264c05`
- [x] M0-03 project scaffold: manage.py + config/ package; `manage.py check` passes — `ab8042c`
- [x] M0-04 settings split w/ DATABASE_URL/REDIS_URL convention; production raises without env vars — `ab8042c`
- [x] M0-05 the 12 Blueprint §1 app packages with AppConfig `name="apps.X"` — `eb6fb53`
- [x] M0-06 trivial test + pytest.ini; `pytest -q` green locally (sqlite fallback) — `4a57fee`
- [x] M0-07 docker-compose.dev.yml up → postgres/redis/emqx healthy — `17ae4d2`
- [x] M0-08 multi-stage Dockerfile builds (`docker build ./backend`) — `d6a4c13`
- [x] M0-09 docker-compose.ci.yml test run GREEN ← **M0 exit gate** — `ff8c942` (3 passed @100% cov in-container)
- [x] M0-10 .env.example documents every settings-read env var; initdb/ placeholder — `54b669a`
- [x] M0-11 commit sweep + demo recorded per WALKTHROUGH — this commit (transcript: `versions/v0.1/DEMO-M0.md`; constraints C-03/C-05/C-06 logged)

## M1 — full schema + roles + RLS

- [ ] M1-01 Compartment 1–2 models verbatim from database.md
- [ ] M1-02 Compartment 3: telemetry hypertable + catalog + aggregates + flock_cycles (+placement_count/day_old_weight_g)
- [ ] M1-03 Compartments 4–7 incl. system_configuration seeds, maintenance_mutes, legal_hold columns
- [ ] M1-04 D22/D23 tables: weight samples, feed/water intake, dashboard_configs, biosecurity_events
- [ ] M1-05 initdb scripts: 00_extensions / 01_roles (5 roles) / 02_rls
- [ ] M1-06 RLS migration over every tenant table (policy list script-checked vs database.md)
- [ ] M1-07 seed data: metric catalog (8 keys), system_configuration, tier quotas, geo sample
- [ ] M1-08 app connects as morghdari_app only; tenant_context() helper; fail-closed proven
- [ ] M1-09 tests/test_rls.py GREEN in CI ← **M1 exit gate**
- [ ] M1-10 scripts/simulate_gateway.py scaffold (--help works)
- [ ] M1-11 DoD sweep: CHANGELOG + tag v0.1 + demo recorded

## Version gate

- [ ] v0.1 EXIT DEMO performed/recorded → STOP for owner retrospective before v0.2
