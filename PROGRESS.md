# PROGRESS — v0.1 Walking Skeleton + Schema/RLS

> EN: Live mirror of `versions/v0.1/TASKS.md`. A task is done when its ACCEPTANCE
> passes (not when code "looks right"). Each line links to the commit that closed it.
>
> فارسی: فهرست زندهٔ وظایف نسخهٔ v0.1 — هر وظیفه فقط با قبولیِ «پذیرش» بسته می‌شود.

## M0 — walking skeleton

- [x] M0-01 git branch `v0.1-m0` created; `backend/` folder exists — `bfb4b93`
- [x] M0-02 requirements per Blueprint §2 pins (`pip list` shows psycopg 3.x) — `9ad0228`
- [x] M0-03 project scaffold: manage.py + config/ package; `manage.py check` passes — `8a2b13d`
- [x] M0-04 settings split w/ DATABASE_URL/REDIS_URL convention; production raises without env vars — `8a2b13d`
- [x] M0-05 the 12 Blueprint §1 app packages with AppConfig `name="apps.X"` — `076d2b8`
- [x] M0-06 trivial test + pytest.ini; `pytest -q` green locally (sqlite fallback) — `7ae6875`
- [x] M0-07 docker-compose.dev.yml up → postgres/redis/emqx healthy — `030a966`
- [x] M0-08 multi-stage Dockerfile builds (`docker build ./backend`) — `817afdb`
- [x] M0-09 docker-compose.ci.yml test run GREEN ← **M0 exit gate** — `e089ef7` (3 passed @100% cov in-container)
- [x] M0-10 .env.example documents every settings-read env var; initdb/ placeholder — `d26fd58`
- [x] M0-11 commit sweep + demo recorded per WALKTHROUGH — this commit (transcript: `versions/v0.1/DEMO-M0.md`; constraints C-03/C-05/C-06 logged)

## M1 — full schema + roles + RLS

- [x] M1-01 Compartment 1–2 models verbatim from database.md — `4c7a9be`
- [x] M1-02 Compartment 3: telemetry hypertable + catalog + aggregates + flock_cycles (+placement_count/day_old_weight_g) — `c7de647` (compression dropped per C-11, fix `3d2c2ec`)
- [x] M1-03 Compartments 4–7 incl. system_configuration seeds, maintenance_mutes, legal_hold columns — `0c3c864`
- [x] M1-04 D22/D23 tables: weight samples, feed/water intake, dashboard_configs, biosecurity_events — `338befb`
- [x] M1-05 initdb scripts: 00_extensions / 01_roles (5 roles) / 02_rls — `3209bc5`
- [x] M1-06 RLS migration over every tenant table (policy list script-checked vs database.md) — `ca6126d` (parity 35/35)
- [x] M1-07 seed data: metric catalog (8 keys), system_configuration, tier quotas, geo sample — `4d484d0`
- [x] M1-08 app connects as morghdari_app only; tenant_context() helper; fail-closed proven — `8b67bdf`
- [x] M1-09 tests/test_rls.py GREEN in CI ← **M1 exit gate** — `291e5b7` (14 passed @84.6% cov in-container)
- [x] M1-10 scripts/simulate_gateway.py scaffold (--help works) — `88b1ecf`
- [x] M1-11 DoD sweep: CHANGELOG + tag v0.1 + demo recorded — this commit (`versions/v0.1/DEMO-M1.md`)

## Version gate

- [x] v0.1 EXIT DEMO performed/recorded → STOP for owner retrospective before v0.2 — `cee9210` tag `v0.1` (transcripts: DEMO-M0.md, DEMO-M1.md; this tick is post-tag tidy)

## M2 — auth & tenancy (v0.2)

> EN: Live mirror of `versions/v0.2/TASKS.md` as updated by decisions D1–D7
> (backend_docs Open Items Tracker §4j). The doc-first batch (M2-00a..c) and the
> contract gate (M2-07a) closed BEFORE any code, per the agreed ritual.
>
> فارسی: آینهٔ زندهٔ تسک‌های نسخهٔ ۰٫۲؛ کارهای سند-اول و گیتِ قرارداد پیش از هر کدی بسته شدند.

- [x] M2-00a Security §4.1 canon: branch order ①–④, verify-once claims delivery, tx topology — backend_docs `cc234cd`
- [x] M2-00b claim semantics doc-first: ordered active-org pick, union roles, parked selector, login/refresh key asymmetry — backend_docs `406c006` + `cc234cd`
- [x] M2-00c offboarding semantics + Tracker §4j record + §6 parked rows — backend_docs `8a3b253`
- [x] M2-07a **GATE** contract v1.4.3 diff published + owner sign-off — backend_docs `406c006`, cleared 2026-08-25
- [x] M2-01 SIMPLE_JWT settings block (15m access / 7d refresh; rotation+blacklist OFF forever; no token_blacklist app; test SECRET_KEY ≥32B for HS256) — `b130cad`
- [x] M2-02 claims serializer: oldest-active-org pick (401 on zero), sorted role union, staff-flag-when-true — `2766648`
- [x] M2-03 login/refresh endpoints per contract keys + CachedJWTAuthentication registered (stash fallback verified) — `bd75633`
- [x] M2-04 no-rotation proof: same refresh reused twice ⇒ two valid accesses; blacklist app unreachable; tampered signature 401 — `fa196c4`
- [x] M2-05 TenantOrgMiddleware: verify-once stash, branch order ①–④, whole-view atomic GUC, fail-closed proofs on real Postgres — `bb38928` ← mid-round checkpoint cleared
- [x] M2-06 RBAC choke point: require_role/RequireRole + farm/saloon/province narrowing truth table — `90e710a`
- [x] M2-07 org-member endpoints per contract v1.4.3: owner-exclusive writes, unusable-password invite, reactivation, soft-off deactivate — `cb33f65`
- [x] M2-08 support-session endpoint: staff-not-superuser, mandatory incident, 900s scoped token via branch ①, SUPPORT_ORG_ENTER audit before return — `b2c7073` (staff org-less login encoded doc-first: docs `ba2384b`)
- [x] M2-09 offboarding chain proven end to end: refresh re-derives claims → org-less token → HTTP 200 empty reads; GUC dies at real commit — `98f9e27`
- [x] M2-10 RBAC matrix: 18 parametrized role×endpoint verdicts on the real middleware path — `db74ee8`
- [x] M2-11 DoD sweep: CHANGELOG [v0.2], DEMO-M2.md, README look-back answers, HANDOFF v0.2 block, final CI gate 91 passed @87.25%, annotated tag `v0.2` — this commit

## Version gate (v0.2)

- [x] v0.2 EXIT DEMO recorded (`versions/v0.2/DEMO-M2.md`); risk killed = app-layer isolation & role enforcement; STOP for owner review before v0.3 — `676014b` + tag `v0.2`

## M3 — ingestion + fake gateway (v0.3)

> EN: Live mirror of `versions/v0.3/TASKS.md`. Decisions Q1–Q6 recorded in
> backend_docs Tracker §4k (all approved 2026-08-25). Contract untouched (v1.4.4).
>
> فارسی: آینهٔ زندهٔ تسک‌های نسخهٔ ۰٫۳؛ شش تصمیمِ برنامه‌ریزی در سند ردیابی ثبت شد.

- [x] M3-01 subscriber package: shared group `$share/telemetry_workers/`, qos=1, persistent session, required unique client id — `6d0c866`
- [x] M3-02 topic parser + identity cross-check + pack→slot resolver (cache-first 24h, DB fallback, UNKNOWN_SLOT DLQ) + DLQ writer — tests written FIRST — `cf00cc1`
- [x] M3-03 dedup guard `SET NX EX 90000` (gateway-level key) + schema/device_ts gates — `3ed8f40`
- [x] M3-04 enrich → XADD per reading to realtime/historical lane by `is_buffered`; flat fields incl `recv_ts`+`trace_id`; raw JSON intact; resolve-ALL-before-publish — `9328a8b`
- [x] M3-07 stream trimming (~100k approximate) on the write path, both lanes — `9328a8b`
- [x] M3-05 Celery consumers: XACK strictly after commit, per-org atomic+tenant_context batches, deterministic recv_ts idempotency (crash-redelivery → exactly one row), beat 10s kick — this commit
- [x] M3-06 historical lane: pacing from system_configuration every cycle (global sleep + sliding per-org window) — this commit
