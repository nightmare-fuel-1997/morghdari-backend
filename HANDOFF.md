# HANDOFF — v0.1 accomplished & closed

> **v1.7.2 UPDATE (2026-09-12):** contract v1.7.2 (follow-through CORE F1–F15 +
> member role change, erratum ratified) sealed on branch `docs/openapi-annotations`,
> commits `a2c2caa` + `a3fe730` + `fa0eda9`, tag `v1.7.2`. Acceptance from the exact
> tagged tree: migrate-gate DEPLOYABLE (73 migrations), 568 passed / 3 skipped @
> 89.73% on PG/Timescale + Redis, module gate GREEN, trivy 0 HIGH/CRITICAL.
> Notable: cold-DB `UndefinedTable` on `saloon_telemetry` found by the ritual and
> fixed in `fa0eda9` (test-side DDL guard — warm reused DBs had masked it). NEXT:
> v0.8 planning on owner go. Push waived — no remote.

> **v0.3 UPDATE (2026-08-26):** v0.3 (ingestion + fake gateway, M3) is CLOSED
> on branch `v0.3-m3`, tag `v0.3`. Truth files: `versions/v0.3/{TASKS,README,
> DEMO-M3}.md`, CHANGELOG `[v0.3]`, Tracker §4k (decisions Q1–Q6), CONSTRAINTS
> C-14/C-15. Contract still v1.4.4 (no surface added in v0.3). Key mechanics:
> subscriber `$share/telemetry_workers/` → pipeline gates → Redis streams →
> Celery consumers (XACK-after-commit, per-org RLS batches) → saloon_telemetry;
> read API `GET /saloons/{id}/telemetry/`; simulator storms via
> `--replay-hours`. NEXT: v0.4 planning on owner go (alerts engine) — physical
> gates unchanged (wal-g bake-in M8, fsync benchmark pre-go-live).

> **v0.2 UPDATE (2026-08-25):** v0.2 (auth & tenancy, M2) is CLOSED on branch
> `v0.2-m2`, tag `v0.2`. Truth files now include `versions/v0.2/{TASKS,README,
> DEMO-M2}.md` and CHANGELOG `[v0.2]`. Resume ritual unchanged; HEAD/tip values
> below are v0.1-historical — trust `git log` over them. Contract canon is
> `backend_docs/API Contract (OpenAPI).yaml` at v1.4.4; decisions D1–D7 +
> errata in backend_docs Open Items Tracker §4j; C-14 pre-registered
> (deactivated-member reads = HTTP 200 empty — correct, do not "fix").
> NEXT: v0.3 planning on owner go (telemetry ingest per versions/v0.3).

> EN: Everything a fresh session (or a new developer) needs to know about what
> v0.1 delivered and how to continue. Written 2026-08-24 at version close.
> فارسی: هرچه یک نشستِ تازه برای ادامه لازم دارد؛ نوشته‌شده در بستنِ نسخهٔ ۰٫۱.

## 0. Resume ritual (do these FIRST)

1. `git log --oneline | head` — HEAD should be `367c524`, tag `v0.1` on `cee9210`.
2. Start Docker Desktop manually if `docker info` fails; engine ready ~5 s.
3. Truth files: `PROGRESS.md` (tasks), `CONSTRAINTS.md` (constraints),
   `CHANGELOG.md` (what/when), `versions/v0.1/{USAGE,RETROSPECTIVE}.md`.
4. Python is ALWAYS `backend/.venv/Scripts/python.exe` (py 3.12); plain
   `python` is 3.11. Run pytest from inside `backend/`.

## 1. What v0.1 accomplished

**The risk killed:** tenant isolation is now enforced by PostgreSQL itself —
no code path can leak one farm's rows to another without defeating FORCE RLS.

### M0 — walking skeleton (`bfb4b93` … `185601d`)
- Repo + branch + per-task commit discipline; requirements pinned to Blueprint §2.
- Four-module settings split; `DATABASE_URL`/`REDIS_URL` single-env convention;
  production refuses to boot without secrets.
- The 12 Blueprint §1 app packages with dotted AppConfig names.
- Multi-stage non-root Dockerfile (`builder/test/runtime`) + `.dockerignore`.
- `docker-compose.dev.yml` (timescale 2.29.2-pg16 / redis / emqx) and an isolated
  CI compose; **M0 gate green** (3 passed @100% cov in-container).
- `.env.example`; `initdb/00_extensions.sql`.

### M1 — full schema + roles + RLS (`4c7a9be` … `cee9210`)
- **45 documented tables transcribed verbatim** across ten apps (incl. D22/D23
  production records, maintenance_mutes, legal_hold columns, tier quotas,
  metric catalog). A db_table-vs-doc diff guards against transcription omissions.
- TimescaleDB hypertables live: `saloon_telemetry` (time + org space partition)
  with 180-day retention; `metric_hourly_aggregates` permanent.
- Five database roles via `initdb/01_roles.sql`; ENABLE+FORCE+`tenant_isolation`
  over the org-scoped tables via `02_rls.sql` + a frozen migration; parity script
  proves doc ↔ initdb ↔ migration match 1:1 (**36/36** after C-10).
- Seeds idempotent: 8 metric keys, 26 config keys, 3 tier quotas, geo sample.
- `tenant_context()` GUC helper; `test_rls.py` proves fail-closed/scoped/no-leak;
  **M1 gate GREEN in containers** (16 passed @84% cov).
- Gateway simulator scaffold (`--help` only; real publish = v0.3).

### Post-gate additions (owner-signed-off)
- **API docs**: `/api/schema/` + Swagger UI `/api/docs/` + ReDoc `/api/redoc/`
  (drf-spectacular; empty-by-honesty until endpoints arrive v0.2+).
- `versions/v0.1/USAGE.md` plain-language usage guide.
- `versions/v0.1/RETROSPECTIVE.md` reality-vs-docs inventory + decision card.

## 2. Decisions locked (retrospective, all applied & synced docs-wide)

| # | Decision | Outcome |
|---|---|---|
| C-11 | compression vs RLS on `saloon_telemetry` | Security wins permanently for v0.x: RLS ON / no columnstore / retention 180 d |
| C-08 | un-executable FK on `gateway_sensor_mapping` | REFERENCES dropped from doc; integrity owned by provisioning writer |
| C-10 | acks missing org_id | Added doc-first; model + backfill migration + policy lists; parity 36/36 |

Plus C-12's NULLIF-hardened policy template propagated to every policy source,
and C-13's `-p morghdari_ci` isolation baked into all gate commands.

## 3. Current state

- Branch `v0.1-m0`, single branch, clean tree; annotated tag **`v0.1`** → `cee9210`.
- Dev stack healthy locally; CI gate command:
  `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build test`
- Commit history voice-neutralized by owner request (message-only rewrite;
  trees untouched; old hashes in older documents were repointed).
- Untracked `.opencode/` + `AGENTS.md` are NOT project files — owner's call.

## 4. Next step (owner-gated)

**v0.2 = JWT auth + first API endpoints** per `versions/v0.2/TASKS.md`.
Starts ONLY on the owner's explicit "start v0.2" — agreed ritual:
conclusions ✅ → docs revised ✅ → next version ⏸ awaiting go.
