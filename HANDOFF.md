# HANDOFF — continue v0.1 build in a fresh chat

> EN: Everything a new session needs to resume without re-deriving context.
> فارسی: هر آنچه یک نشست تازه برای ادامهٔ ساخت لازم دارد — ابتدا «Resume ritual»، بعد وضعیت.

## Resume ritual (do these FIRST, in order)

1. `cd M:/projects/poultry_the_main_by_ai && git log --oneline | head` — confirm last commit matches "State snapshot" below.
2. `docker info --format '{{.ServerVersion}}'` — if it fails, start Docker Desktop (`start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"`), poll until up (~5 s here).
3. Read `PROGRESS.md` (task truth) and `CONSTRAINTS.md` (known friction) before writing any code.
4. Python is ALWAYS `backend/.venv/Scripts/python.exe` (py 3.12). Never plain `python` (=3.11).

## Mission

Build Morghdari version-by-version per the spiral roadmap. **This round = v0.1 ONLY**
(walking skeleton M0 + schema/RLS M1), then STOP at the exit-demo gate for owner
retrospective before starting v0.2. Specs of record: `versions/v0.1/*.md` +
`backend_docs/database.md` (transcribe VERBATIM — doc-first rule) + Blueprint §1–§8.

Standing user decisions:
- Repo rooted at project root (alongside `versions/`); detailed commit per TASKS.md ID.
- Owner console = separate static HTML/JS app in `owner-console/`, EXCLUSIVE to the
  owner, never shipped, not part of any API contract (real frontend = another team).
- Comments bilingual: English teaching comments (WHY + doc-decision refs) plus a
  Persian summary block at top of every file.
- Task truth = PROGRESS.md checkboxes; constraint truth = CONSTRAINTS.md.

## State snapshot (2026-08-24)

Branch `v0.1-m0`. Commits present:
```
<verify with git log>  M0-06: walking-skeleton tests + pytest.ini — 3 passed @100% cov
                       M0-05: the 12 Blueprint §1 app packages
                       M0-03 + M0-04: scaffold + four-module settings split
                       M0-02: requirements per Blueprint §2 pins
                       chore: track versions/, LF endings
                       M0-01: repo init + docs
```
Working tree may contain **backend/Dockerfile written but NOT committed/built**.

DONE & verified: toolchain (git 2.55, Docker 29.7.2 + compose v5.4, py3.12 via
`py -V:3.12`); venv `backend/.venv` with test reqs (Django 5.0.14, psycopg 3.3.4);
dev compose UP and healthy (postgres/redis/emqx); `manage.py check` clean;
pytest green @100% coverage (sqlite fallback).

NOT DONE yet (in order):
1. **M0-08** `.dockerignore` + `docker build ./backend` acceptance + commit (Dockerfile exists on disk — review, build, commit)
2. **M0-09** `docker-compose.ci.yml` (postgres `morghdari_test` + redis + `test:` service building ./backend, DATABASE_URL set, target: test stage) → `docker compose -f docker-compose.ci.yml run --rm test` GREEN ← **M0 exit gate**
3. **M0-10** `.env.example` + `initdb/00_extensions.sql` (CREATE EXTENSION IF NOT EXISTS timescaledb)
4. **M0-11** sweep: add CONSTRAINTS rows C-03 (CRLF→.gitattributes), C-04 (compose `version:` obsolete warning), C-05 (HEALTHCHECK unhealthy by design until /health exists in v0.6); tick PROGRESS boxes; demo transcript note
5. **M1-01..11** per versions/v0.1/TASKS.md: models VERBATIM from database.md all compartments (~51 tables incl. maintenance_mutes, legal_hold cols, province_id, ~26 system_configuration seeds, tier quotas, metric catalog 8 keys); telemetry/aggregates managed=False + RunSQL hypertable/compression/retention (guard vendor=='postgresql'); initdb 01_roles.sql (5 roles) + 02_rls.sql; RLS migration; tenants/context.py tenant_context(); seeds idempotent; conftest+factories; tests/test_rls.py via session fixture applying 02_rls.sql + SET ROLE morghdari_app (cross-org=0, insert blocked, unset GUC=0, no leak); scripts/simulate_gateway.py --help; CHANGELOG + tag **v0.1**
6. **owner-console/**: README (ownership/separation) + index.html/style.css/app.js — login placeholder + live `/api/v1/health/` panel + serve.bat; bilingual labels EN/فا

## Environment quirks

| Fact | Detail |
|---|---|
| Python | default 3.11 — use `py -V:3.12`; venv at `backend/.venv` |
| Docker | Desktop must be started manually; engine ready ~5 s; compose warns `version:` obsolete (kept verbatim, doc-first) |
| Line endings | repo forces LF via .gitattributes |
| Dev DB creds | `morghdari_dev/dev_password_only`, redis `dev_redis_password` (mirror docker-compose.dev.yml) |
| Git identity | check `git config user.name/email` before committing |

## Hard rules (from user + docs)

- Acceptance closes tasks, never looks-right; PROGRESS.md ticked only after proof.
- One detailed commit per task ID (subject `Mx-nn: ...`, body explains WHY + doc ref).
- No scope creep past v0.1 (hardening rule); new ideas → Tracker notes, not code.
- After tag v0.1: STOP and report constraints + retrospective input. Do NOT start v0.2.
