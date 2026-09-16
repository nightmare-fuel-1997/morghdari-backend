# Migration index — 2026-09-16

This file records work migrated **into** this repository from the `morghdari-vps-deploy`
working copy (`M:\projects\poultry_vps_deploy`, commit `f7ac8ec`), so the progress made
there is not lost if that folder is later removed.

## What was migrated

| File | Change | Commit |
|---|---|---|
| `scripts/run_acceptance.sh` | `-T` added to the three `docker compose run` invocations — a Windows/PowerShell TTY kept the pytest container alive after the suite finished, stalling the ritual before teardown / gate 3 | `491daf1` |
| `docker-compose.ci.yml` | `timeout 1800` bound on the pytest gate — a wedged run now FAILS the gate instead of hanging forever | `491daf1` |
| `docker-compose.prod.yml` | `postgres mem_limit` 4g → 1200m, `celery-worker --concurrency` 4 → 2 — sizing for a 2-core / 4 GB host | `491daf1` |

Diff: 3 files, +14 / −6. Local commit only — **not pushed**.

## What was deliberately not migrated

- The **VPS deploy layer** (`docker-compose.vps.yml`, `.env.vps.example`, `README_VPS.md`,
  `backend/scripts/ci_wait_db.py`) — that is the `morghdari-vps-deploy` repository's own
  material; this repository is not the VPS deploy target.
- The **live QA harness** (`scripts/qa_*.py`, `qa-test-plan.html`, `scripts/qa_results.jsonl`)
  — snapshot-specific; it drives the deployed stack.
- Caches, scratch and secrets — regenerable or deliberately excluded.

Full per-item classification: see the audit pack
`DELIVERY\2026-09-16-parent-sync\MIGRATION-MANIFEST.csv` in the AutoCoder workspace.

## Verification

- Marker parity verified by reading this repository alone: `run -T` ×3 (L49/L52/L55),
  `timeout 1800` ×1 (L162), `mem_limit: 1200m` ×1 (L122), `--concurrency=2` (L280).
- `bash -n scripts/run_acceptance.sh` → 0; `docker compose config -q` on both composes → 0.
- Pre-existing content preserved: `git show 7cca9a2:<file>` for any of the three.

## Rollback

```
git reset --soft HEAD~1                              # unstage/undo the migration commit
git checkout 7cca9a2 -- scripts/run_acceptance.sh    # or restore one file
```
