# v1.2 — Lightweight analytics *(beyond v1.1, §4n 2026-09-04)*

> **Canonical plan:** `../../VERSIONS.md` §v1.2. **Not started — no code.**
> **Status pointer:** this stub exists only so `versions/` covers v0.1–v1.2;
> the build contract (TASKS/TUTORIAL/WALKTHROUGH) has not been written.

## Scope (from canonical VERSIONS.md)

- Lightweight analytics via `analytic_insights` computed on existing aggregates
  (no warehouse, no new pipeline)
- **Batch 2 inputs** (Tracker §4p, same v1.1 train): `org_daily_usage` +
  `notification_daily_stats`. Spec: `../../docs/data-aggregates-v1.md`

## Decisions

Tracker §4n + §4p in backend_docs `Open Items Tracker.md`.

## Exit demo (canonical)

At least one insight rendered from aggregates with traceable computation.
