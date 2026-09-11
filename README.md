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
| `backend/openapi.yaml` | **Canonical contract** (hand-written, the `implemented == contract` gate) |
| `backend/openapi.generated.yaml` | Generated mirror of the implemented API (drf-spectacular) |

## API documentation

Two documents, deliberately:

- `backend/openapi.yaml` is the **contract** — hand-written, frozen per wave, and
  enforced by `backend/tests/test_openapi_contract.py` (`implemented == contract`).
  Edit it only in a ratification commit.
- `backend/openapi.generated.yaml` is the **mirror** of what the code actually
  serves, generated from the `@extend_schema` annotations. Browsable at
  `/api/docs/` (Swagger UI) and `/api/redoc/`, machine-readable at `/api/schema/`.
  Regenerate it whenever a route or its annotated shape moves:

  ```bash
  python backend/manage.py spectacular --file backend/openapi.generated.yaml
  ```

`backend/tests/test_openapi_schema.py` is the gate: generation must emit **zero
warnings and zero errors** (no view may fall back to a guessed serializer), the
document must validate as OpenAPI 3.0, only the canonical prefixes are
documented, every operation carries a summary, and the committed mirror must
cover the same routes as the code.

فارسی: قراردادِ دست‌نویس `openapi.yaml` است و آینهٔ تولیدشده از کد در
`openapi.generated.yaml`؛ مستندات زنده در `/api/docs/` و دروازهٔ صحتِ آن تست
`test_openapi_schema.py` است (صفر هشدار/خطا + اعتبار OpenAPI).

## Specs of record

- `backend_docs/database.md` — schema DDL is transcribed **verbatim** (doc-first rule).
- `backend_docs/Implementation Blueprint — How to Build.md` — stack pins, layout, milestones.
- `backend_docs/Open Items Tracker.md` §4i — resolution decisions (rotation disabled, GS-HUMAN-SAFETY rejected, …).
