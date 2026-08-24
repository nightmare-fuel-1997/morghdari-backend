# HANDOFF — v0.1 CLOSED; retrospective resolved; awaiting v0.2 go

> EN: This file used to be a resume checklist for the v0.1 build. That build is
> COMPLETE (tag `v0.1`), the owner retrospective is RESOLVED, and all doc
> revisions have landed. This file now only records the standing facts.
> فارسی: ساخت v0.1 کامل شد؛ جمع‌بندی مالک بسته شد و بازنویسی داکیومنت‌ها انجام
> شد. این فایل فقط وضعیتِ ایستا را نگه می‌دارد.

## Standing environment facts

- Python is ALWAYS `backend/.venv/Scripts/python.exe` (py 3.12; plain `python` = 3.11).
- Docker Desktop starts manually (`docker info` to check).
- CI gate MUST isolate its project namespace:
  `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build test`
  (without `-p`, CI replaces the dev stack's containers — CONSTRAINTS C-13).
- Dev stack: `docker compose -f docker-compose.dev.yml up -d`.

## Where truth lives

| Thing | File |
|---|---|
| Task status | PROGRESS.md (all M0+M1 boxes ticked) |
| Constraints log | CONSTRAINTS.md (C-01..C-13; C-08/C-10/C-11/C-12 carry resolutions) |
| Reality-vs-docs report | versions/v0.1/RETROSPECTIVE.md |
| What changed when | CHANGELOG.md ([Unreleased] = retrospective resolutions) |
| Demo transcripts | versions/v0.1/DEMO-M0.md, DEMO-M1.md |

## Next step (owner-gated)

v0.2 planning (JWT auth + first API endpoints per versions/v0.2/TASKS.md)
starts ONLY on the owner's explicit go — the agreed ritual was:
conclusions → docs revised (DONE) → next version.
