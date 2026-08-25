# HANDOFF — v0.1 accomplished & closed

> EN: Everything a fresh session (or a new developer) needs to know about what
> v0.1 delivered and how to continue. Written 2026-08-24 at version close.
> فارسی: هر آنچه نسخهٔ ۰٫۱ تحویل داده است و چطور ادامه دهید — برای نشست تازه.

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
