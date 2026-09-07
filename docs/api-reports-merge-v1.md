# Daily Reports Payload Merge — Additive Extension (Contract v1.6.0, App v0.6 scope)

> **Status:** DRAFT 2026-09-07 · **Additive-only** — zero `ALTER` on frozen tables; one new 1:1
> side table (`daily_report_extras`); every v1.5.0 field, path, verb and envelope stays valid.
> **Owner decisions:** `Open Items Tracker.md` §4s (R-01 auth unchanged · R-02 merge · R-03 vehicle).
> **Canonical homes:** this file (merged surface) · `API Contract (OpenAPI).yaml` v1.5.0 report
> paths (extended at ratification) · `database.md` §Daily Reports (side table added) ·
> history: `CHANGELOG.md [Unreleased → 1.6.0]`.
> **Source of the Flutter side:** frontend repo `docs/api_requirements.md` §3.1 (Daily Report
> Form payload) + `docs/api_calls.md` (2026-09-07).

---

## 1. What problem this closes

The shipped Flutter UI has a Daily Report Form whose payload (`bird_count`, `casualties_today`,
`ventilation_status`, `health_status`, `checklist`, `is_draft`, …) does not match the v1.5.0
`POST /saloons/{saloon_id}/reports/` body (`mortality_count`, `feed_kg`, `water_l`,
`avg_weight_g`, `medications`). Owner decision §4s R-02: **merge, discard nothing.**

Merge rules (locked):

1. **Canonical names win on output.** Responses always use the v1.5.0 canonical keys.
2. **Flutter names are accepted input aliases.** The server normalizes aliases to canonical
   fields on write; sending either (or both, if equal) is valid. Both sent with different
   values → `400 {"error":"conflicting fields: <alias> vs <canonical>"}`.
3. **Data the backend lacked gets new columns** in a side table — never an `ALTER` on the
   frozen `daily_reports` (same pattern as BN-02 `saloon_profile`).
4. **State machine unchanged.** DRAFT→SUBMITTED→APPROVED/REJECTED + FLAGGED branch; Persian
   status literals are input aliases of the English enums; client-side localization stays
   client-side.

---

## 2. Merged request body — `POST /saloons/{saloon_id}/reports/`

Required fields unchanged: `flock_id`, `report_date`. Everything else optional; omitted
fields stay `NULL` / default.

| Canonical key | Accepted alias (Flutter) | Type | Notes |
|---|---|---|---|
| `flock_id` | — | int | required, unchanged |
| `report_date` | — | date (Asia/Tehran) | required, unchanged |
| `mortality_count` | `casualties_today` | int ≥ 0 | same fact, two names |
| `feed_kg` | `feed_consumed_kg` | number | |
| `water_l` | `water_consumed_l` | number | |
| `avg_weight_g` | `avg_sample_weight_g` | number | |
| `notes` | — | string | |
| `status` | `is_draft` (bool) | enum `DRAFT\|SUBMITTED` | `is_draft: true` → `DRAFT`, `false` → `SUBMITTED`; conflicting with explicit `status` → 400 |
| `medications[]` | — | array | unchanged v1.5 semantics (medication row → FLAGGED + medical ticket + WS to doctor) |
| `bird_count` | `bird_count` | int ≥ 0 | **new** — birds at report time |
| `avg_temperature_c` | `avg_temperature_c` | number | **new** — environment snapshot |
| `avg_humidity_pct` | `avg_humidity_pct` | number | **new** |
| `ventilation_status` | `ventilation_status` | enum `STABLE\|NEEDS_REVIEW\|PROBLEM` | **new**; Persian literals accepted: `پایدار`→STABLE, `نیازمند بررسی`→NEEDS_REVIEW, `مشکل‌دار`→PROBLEM |
| `health_status` | `health_status` | enum `NORMAL\|WARNING\|CRITICAL` | **new**; `عادی`→NORMAL, `هشدار`→WARNING, `بحرانی`→CRITICAL |
| `checklist` | `checklist` | object `{feed, water, device}` booleans | **new**; stored as three booleans; see §4 enforcement |
| `farm_slug`, `salon_slug` | — | — | **canonical body: not accepted** (path carries the saloon). Accepted only on the compat write alias (§5) |

## 3. Merged response — report objects (list + single)

All v1.5.0 list fields keep their names and positions; the new fields are appended (additive,
old clients unaffected):

| Key | Source | Present in |
|---|---|---|
| `id`, `report_date`, `mortality_count`, `feed_kg`, `water_l`, `avg_weight_g`, `status` | `daily_reports` | list + single (unchanged) |
| `bird_count`, `avg_temperature_c`, `avg_humidity_pct`, `ventilation_status`, `health_status`, `checklist: {feed, water, device}` | `daily_report_extras` | list + single (**new**) |
| `medications[]` (`medication_name`, `dosage`, `frequency`) | `report_medication_usages` | single only |
| `notes` | `daily_reports` | single only |
| `reported_by` (= `submitted_by`), `reported_at` (= `submitted_at`) | `daily_reports` | single only (aliases of the canonical keys, returned alongside for the Flutter calendar) |
| `is_completed` | derived | list + single (**new**) — `true` when `status ∈ {APPROVED}`; the Flutter calendar/dashboard "done" flag |
| review fields (`reviewed_by`, `review_note`, …) | `daily_reports` | single only, role-gated as v1.5 |

**New canonical path (additive):** `GET /saloons/{saloon_id}/reports/{report_date}/` — single
report by calendar day (the Flutter calendar day-selection). 404 when absent.

**PATCH /reports/{report_id}/** gains the same optional fields as POST (canonical + aliases,
minus `flock_id`/`report_date` which are immutable). Edit time-lock (same Tehran day, cutoff
00:00) and status machine unchanged.

## 4. Checklist enforcement (preserves the Flutter rule)

The Flutter form requires all three checks before submit. Server mirrors it:

- `DRAFT` — any checklist state allowed.
- Transition to `SUBMITTED` (via POST with `status`/`is_draft` or PATCH) with any of
  `checklist.feed / water / device == false` → `400 {"error":"checklist incomplete"}`.
- System Admin / Support whitelisted edits are exempt (same exemption as the edit time-lock).

## 5. Compat write/read aliases (supersedes C-09 "no compat writes" for reports only)

Same prefix, auth, and error contract as the compat layer (`docs/api-compat-flutter-v1.md` §3/§8).
Slugs resolve via `legacy_slug_map`; `reporter` = JWT user (never client input).

| # | Route | Maps to | Notes |
|---|---|---|---|
| W1 | `GET /api/compat/v1/farms/{farmSlug}/salons/{salonSlug}/reports/` | `GET /saloons/{id}/reports/` | query `from`, `to`, `status` (v1.5 enum) — this is the Flutter calendar/history list |
| W2 | `GET /api/compat/v1/farms/{farmSlug}/salons/{salonSlug}/reports/{report_date}/` | new single-report GET | calendar day selection |
| W3 | `POST /api/compat/v1/reports/` | `POST /saloons/{id}/reports/` | body may carry `farm_slug` + `salon_slug` (resolved, must agree) and every Flutter alias from §2 |
| W4 | `PATCH /api/compat/v1/reports/{report_id}/` | `PATCH /reports/{id}/` | aliases accepted |
| W5 | `GET /api/compat/v1/farms/{farmSlug}/reports/?status=incomplete` | list query | `incomplete` = `status ∈ {DRAFT, SUBMITTED}` — the Flutter dashboard queue; native v1.5 callers use the enum values |

## 6. Schema addition (additive — the ONLY DDL in this decision)

```sql
-- ADDITIVE ONLY. One 1:1 side table; frozen daily_reports untouched.
CREATE TABLE daily_report_extras (
    report_id          INT PRIMARY KEY REFERENCES daily_reports(id) ON DELETE CASCADE,
    org_id             UUID NOT NULL REFERENCES organizations(org_id) ON DELETE RESTRICT,  -- RLS tenant_isolation
    bird_count         INT,
    avg_temperature_c  NUMERIC(5,2),
    avg_humidity_pct   NUMERIC(5,2),
    ventilation_status VARCHAR(20) CHECK (ventilation_status IN ('STABLE', 'NEEDS_REVIEW', 'PROBLEM')),
    health_status      VARCHAR(20) CHECK (health_status IN ('NORMAL', 'WARNING', 'CRITICAL')),
    checklist_feed     BOOLEAN NOT NULL DEFAULT FALSE,
    checklist_water    BOOLEAN NOT NULL DEFAULT FALSE,
    checklist_device   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_report_extras_org ON daily_report_extras(org_id);
```

Side-table rows are created lazily on first write carrying a new field (reports with only
v1.5.0 fields get no row — zero overhead for canonical callers).

## 7. What stays frozen / unchanged

- Auth: `POST /api/v1/auth/login/` + `/auth/token/refresh/` exactly as designed (§4s R-01;
  C-09 "no login compat" stands).
- State machine, FLAGGED auto-ticket + WS push to doctor, edit time-lock, vet-review, RLS.
- v1.5.0 request/response shapes: every old client payload stays byte-valid (all new fields
  optional; responses gain keys only).
- The C1–C16 GET-only compat surface.

## 8. Acceptance (docs-side; code gates in the main repo)

- [ ] `daily_report_extras` created by an additive migration; no `ALTER` on `daily_reports`.
- [ ] POST with pure-Flutter body (aliases + `is_draft` + Persian statuses) == POST with
      canonical body → same stored row (alias-normalization test).
- [ ] Both-name conflict → 400; checklist-incomplete SUBMITTED → 400; DRAFT exempt.
- [ ] Calendar flow green: W1 list + W2 single with `is_completed` + `reported_at`.
- [ ] Old v1.5.0 payload replay → identical result as before the merge (byte-compat gate).
- [ ] OpenAPI 1.6.0 ratified with the merged schemas; `CHANGELOG.md [1.6.0]` finalized.
