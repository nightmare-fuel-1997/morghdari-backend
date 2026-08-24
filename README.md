# Morghdari Backend — build log / گزارش ساخت

> EN: Implementation repository for the Morghdari poultry-monitoring platform.
> Built version-by-version from the spiral roadmap in [`versions/`](versions/)
> against the canonical specs in `M:\projects\poultry\backend_docs`.
> The exit demo of each version is the only definition of "done".

## فارسی (خلاصه)
این مخزن، محل ساخت عملی پلتفرم مرغداری است. هر نسخه (v0.1 تا v1.0) طبق نقشهٔ
اسپیرال در پوشهٔ `versions/` و مشخصات مرجع در `backend_docs` ساخته می‌شود؛
معیارِ «تمام‌شده» فقط دموی خروج همان نسخه است.

## Repository map

| Path | What |
|---|---|
| `backend/` | Django project + subscriber service (one image, four commands) |
| `initdb/` | Postgres bootstrap SQL (extensions, roles, RLS policies) |
| `owner-console/` | **Owner-only visual console** — static HTML/JS, never shipped, not part of any API contract. The real frontend is a separate team's product. |
| `versions/` | Build curriculum per version (TASKS / TUTORIAL / WALKTHROUGH / README) — read-only reference |
| `scripts/` | Gateway simulator and helpers |
| `PROGRESS.md` | Live task checklist (mirrors versions/vX/TASKS.md) |
| `CONSTRAINTS.md` | Real-world constraints discovered while building |

## Specs of record

- `backend_docs/database.md` — schema DDL is transcribed **verbatim** (doc-first rule).
- `backend_docs/Implementation Blueprint — How to Build.md` — stack pins, layout, milestones.
- `backend_docs/Open Items Tracker.md` §4i — resolution decisions (rotation disabled, GS-HUMAN-SAFETY rejected, …).
