# Business Numbers Addendum — KPI Extension + Supporting Tables (v1.9.0 scope, App v1.1 ext. II)

> **Status:** DRAFT 2026-09-05 · **Additive-only** — KPI gains optional output fields (old callers
> unaffected); three small new tables; one draft-table amendment (`feed_deliveries.unit_price`,
> that table is itself unratified draft). No `ALTER` on built tables, `database.md` untouched.
> **Canonical homes:** this file (formulas + tables) · decisions: `Open Items Tracker.md` §4r ·
> plan: `VERSIONS.md` §v1.1 (folded into the v1.9.0 train) · history: `CHANGELOG.md [Planned → 1.9.0]`.
> Audit origin: business-numbers review 2026-09-05 (14 numbers checked: 8 complete, 6 gaps —
> 3 of them collected-but-never-returned).

---

## 1. KPI output extension (existing `GET /saloons/{id}/kpis/`, additive fields only)

`metrics` param accepts four new keys alongside the current four. Every new key is NULL until
its inputs exist (D22 NULL-rule — never 0, never an error).

| New metric key | Formula (all NULL-safe) | Inputs (all already stored) |
|---|---|---|
| `water_ratio` | `water_cum_l / feed_cum_kg` (industry band ~1.8–2:1; out-of-band is an early-disease signal, not an alert) | `flock_water_intake` + `flock_feed_intake` |
| `epef` | `(livability_pct × live_weight_kg) / (age_days × fcr) × 100`, `livability_pct = 100 − mortality_pct` | mortality %, latest weight sample, age, FCR |
| `adg` | `(latest_avg_weight_g − baseline_g) / days_elapsed`, baseline = `day_old_weight_g` else first sample | `flock_weight_samples` + `flock_cycles` |
| `uniformity` | latest `uniformity_pct` + `sample_count` behind it (trend rides the existing weight-samples list) | `flock_weight_samples` |
| `vs_standard` | `{weight_delta_pct, fcr_delta}` vs `breed_standards` row for (`breed_type`, age_day); NULL when breed unmatched | §2 table + current KPIs |

Response compatibility: old `metrics` values return byte-identical shapes; default `metrics`
set is UNCHANGED (callers opt into the new keys explicitly).

## 2. New tables (all additive, all `org_id`-scoped + RLS)

| Table | Key columns | Serves |
|---|---|---|
| `saloon_profile` | `saloon_id INT PK REFERENCES saloons(id)`, `org_id UUID`, `area_m2 NUMERIC NULL`, `capacity_birds INT NULL`, `height_m NUMERIC NULL`, `updated_at` — one row per saloon, owner/manager writable via `PATCH /api/v1/saloons/{id}/profile/` (new in §3) | **stocking density** (`live_birds / area_m2`), the one business number whose input never existed (`database.md` saloons carries name only) |
| `breed_standards` | `breed TEXT`, `day SMALLINT`, `target_weight_g NUMERIC`, `target_fcr NUMERIC NULL`, `source TEXT` (e.g. `aviagen-ross308-2024`), `PRIMARY KEY (breed, day)` — GLOBAL seed (no `org_id`, like `sensor_metric_types`) | `vs_standard` deltas. Seed values MUST be taken from the publishers' performance objectives at implementation; any numbers in review drafts are placeholders, not data |
| `vet_withdrawal_rules` | `catalog_item_id INT PK REFERENCES vet_medication_catalog(id)`, `org_id UUID`, `withdrawal_days INT NOT NULL CHECK (≥0)`, `basis TEXT NULL` (label/authority reference), vet-only writes | **withdrawal countdown**: per medication usage, `days_remaining = withdrawal_days − days_since_administration`; report detail gains additive `withdrawal_status: {blocking: bool, items: [{medication, days_remaining}]}` and the evidence pack gains a withdrawal section. Blocking (not advisory): scheduling slaughter inside the window is refused while this ships as warning-first (warn in v1.9, enforce by owner decision later) |

## 3. New + amended routes (all `BearerAuth`, tenant-scoped, `{"error": ...}`)

| # | Method + Route | Role | Notes |
|---|---|---|---|
| B1 | `GET /api/v1/saloons/{id}/profile/` | member of owning org | `200`: profile row or `404 {"error":"no profile yet"}` |
| B2 | `PATCH /api/v1/saloons/{id}/profile/` | manager+ | `{area_m2?, capacity_birds?, height_m?}` (positive numbers; density recomputes downstream, never stored redundantly) |
| B3 | `GET /api/v1/breed-standards/?breed=&day=` | engineer+ | read-only global catalog; `404` on unmatched breed (drives `vs_standard: null`) |
| B4 | `GET /api/v1/vet-catalog/{id}/withdrawal/` | engineer+ | `200`: `{withdrawal_days, basis}` or `404` when no rule set (unset ⇒ excluded from blocking math, flagged in UI as "unknown — assume blocking") |
| B5 | `PUT /api/v1/vet-catalog/{id}/withdrawal/` | doctor | `{withdrawal_days (≥0), basis?}` — vet-only, like the catalog itself |
| B6 (amend) | `feed_deliveries` += `unit_price` | — | draft-table fix in `docs/api-followthrough-v1.md` (§6 row + F34 request `{unit_price?}`): enables feed-cost-per-kg-gain later without another table. Chick placement cost stays out (hatchery is Phase 2) |

Density definition (locked): `density = (placement_count − cumulative_mortality) / area_m2`
(live birds per m², computed per request from snapshot + profile; NULL without a profile row).

## 4. Explicit non-scope

- Slaughter-outcome numbers (carcass/condemnation) — need the processing-plant loop (Phase 2).
- Energy cost accounting, medicine stock quantities — Phase 2.
- Chick cost — hatchery chain is Phase 2.
- Withdrawal ENFORCEMENT on slaughter scheduling — warning-first in v1.9; enforcement needs an owner decision (slaughter scheduling itself is Phase 2).

## 5. Acceptance checklist (docs-side)

- [ ] KPI returns the 4 old keys byte-identical; each new key NULL-safe per D22 rule.
- [ ] `vs_standard` NULL on unmatched breed; seeded only from publisher objectives.
- [ ] Density NULL without a profile row; correct after B2.
- [ ] Unknown-withdrawal medication flags "assume blocking", never silently passes.
- [ ] `feed_deliveries.unit_price` present in the v1.9 spec + draft.
