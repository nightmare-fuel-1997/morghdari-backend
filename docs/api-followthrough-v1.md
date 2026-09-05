# Follow-Through APIs — Endpoint Spec (Contract v1.9.0-draft, App v1.1 scope extension II)

> **Status:** DRAFT 2026-09-05 · **Additive-only** — no `ALTER` on core tables, no change to
> existing paths. Five small new tables (§8); everything else reads/writes frozen tables
> (farm/saloon creation follows the locked rule: **owner creates farms**, Tracker §4d R3-ACTOR-MAP).
> **Canonical homes:** this file (v1.9.0 surface) · decisions: `Open Items Tracker.md` §4q ·
> plan: `VERSIONS.md` §v1.1 · release draft: `docs/releases/v1.9-followthrough-draft.md` ·
> history: `CHANGELOG.md [Planned → 1.9.0]`.
> Sibling specs: `docs/api-ops-v1.md` (v1.7.0) · `docs/api-growth-v1.md` (v1.8.0) ·
> `docs/data-aggregates-v1.md`. Pagination on all lists: v1.7.0 convention
> (`limit`/`offset` → `{results, count}`).

---

## 1. Route table (all `BearerAuth`, tenant-scoped via JWT `org_id` + RLS)

| # | Method + Route | Role | Request | Response |
|---|---|---|---|---|
| F1 | `POST /api/v1/farms/` | owner (locked rule) | `{name, province_id, city_id, village_id?}` | `201`: farm. `400` on unknown geography |
| F2 | `PATCH /api/v1/farms/{id}/` | owner | `{name?}` | `200`: updated. `404` foreign |
| F3 | `DELETE /api/v1/farms/{id}/` | owner | — | `200`: deleted **only when it has no saloons**, else `409 {"error":"farm not empty"}` (RESTRICT philosophy, never cascade) |
| F4 | `POST /api/v1/farms/{id}/saloons/` | manager+ | `{name}` (+ positions seeded empty) | `201`: saloon |
| F5 | `PATCH /api/v1/saloons/{id}/` | manager+ | `{name?}` | `200`: updated (operational state stays on its own endpoint) |
| F6 | `DELETE /api/v1/saloons/{id}/` | manager+ | — | `200` only with no active flock and no telemetry, else `409` |
| F7 | `POST /api/v1/saloons/{id}/positions/` | manager+ | `{label}` (`UNIQUE(saloon_id,label)` stands) | `201`: position. `409` on duplicate label |
| F8 | `PATCH /api/v1/saloons/{id}/positions/{pid}/` | manager+ | `{label?, is_active?}` | `200`. Sensor bindings follow automatically (mapping rows untouched) |
| F9 | `GET /api/v1/alerts/` | engineer+ (org feed) | `farm_id?`, `saloon_id?`, `tier?`, `metric?`, `status?` (`ACTIVE`/`ALL`), `limit`/`offset` | `200`: `{results:[AlertInstance…], count}` — the missing org-wide feed (per-saloon read already exists) |
| F10 | `GET /api/v1/overview/` | any member | none | `200`: owner home `{farms:[{farm_id, name, saloon_count, active_alert_count, highest_tier}], totals:{…}}` (multi-farm, RLS-scoped) |
| F11 | `GET /api/v1/maintenance-mutes/` | manager+ | `saloon_id?`, `active_only?` (default true) | `200`: `{results, count}` over frozen `maintenance_mutes` (first read path for the table) |
| F12 | `POST /api/v1/maintenance-mutes/` | manager+ | `{saloon_id, starts_at, ends_at, reason}` (window ≤72 h, no overlap per saloon) | `201`: mute. `400` on overlap/past window. Never mutes velocity (worker semantics unchanged) |
| F13 | `DELETE /api/v1/maintenance-mutes/{id}/` | manager+ | — | `200`: revoked early (audit row kept) |
| F14 | `PATCH /api/v1/saloons/{id}/flocks/{fid}/close/` | engineer+ | `{end_date}` (≥ start_date, active flock only) | `200`: cycle closed → triggers snapshot finalization + threshold hash persistence (v0.4 Q3 rule). `400` when no active flock |
| F15 | `POST /api/v1/saloons/{id}/flocks/{fid}/mortality/` | engineer+ | `{occurred_on, count (>0), note?}` | `201`: `mortality_records` row (direct entry beside daily-report counts) |
| F16 | `PATCH /api/v1/support-tickets/{id}/` | manager+ | `{status? (OPEN→IN_PROGRESS→RESOLVED→CLOSED, forward-only), assigned_to?}` | `200`: advanced. `400` on backward jump (forward-only workflow, D24-5) |
| F17 | `PATCH /api/v1/farms/{id}/events/{eid}/` | manager+ | `{title?, scheduled_at?, notes?, status? (scheduled/cancelled)}` | `200`. Cancelling stops future reminders (past deliveries kept) |
| F18 | `DELETE /api/v1/farms/{id}/events/{eid}/` | manager+ | — | `200`: cancelled (soft; row kept for audit) |
| F19 | `PATCH /api/v1/vet-catalog/{id}/` | doctor | `{name?, dosage_default?, unit?, notes?}` | `200`. `409` on name clash (same rule as create) |
| F20 | `DELETE /api/v1/vet-catalog/{id}/` | doctor | — | `200`: deactivated (`is_active=false`, never deleted — reports reference it) |
| F21 | `GET /api/v1/saloons/{id}/operational-history/` | member of owning org | `limit`/`offset` | `200`: `{results:[{state, reason, changed_by, at}], count}` (first read path for the history table) |
| F22 | `GET /api/v1/alerts/{alert_id}/acknowledgements/` | member of owning org | none | `200`: ack rows for the alert (first read path; writes already exist) |
| F23 | `GET /api/v1/me/notifications/` | any member (own rows) | `category?`, `limit`/`offset` | `200`: own `notification_deliveries` (first read path for the table) |
| F24 | `POST /api/v1/auth/org/switch/` | member of target org | `{org_id}` | `200`: fresh 15-min access token scoped to that org (unlocks parked D6, §4j — no more stuck-on-oldest-membership). `404` for foreign orgs (identical to missing) |
| F25 | `PATCH /api/v1/org/` | owner | `{name?}` | `200`: renamed (slug/alias rows in `legacy_slug_map` untouched — slugs stay stable) |
| F26 | `GET /api/v1/search/?q=` | any member | `q` (≥2 chars), `limit` (default 20, max 50) | `200`: `{farms:[…], saloons:[…], gateways:[…]}` RLS-scoped name/slot match. `400` on short query |
| F27 | `GET /api/v1/saloons/{id}/turnarounds/` | engineer+ | `limit`/`offset` | `200`: `{results:[{id, flock_id?, started_at, finished_at?, items_done, items_total}], count}` |
| F28 | `POST /api/v1/saloons/{id}/turnarounds/` | engineer+ | `{flock_id?}` (the cycle just closed, if any) | `201`: checklist opened from the 8-task standard catalog (§8), between-flock window enforced (refuses while a flock is active → `400`) |
| F29 | `POST /api/v1/turnarounds/{tid}/items/{key}/sign/` | engineer+ | — | `200`: item signed `{by, at}`. All 8 signed → checklist auto-finishes (audit row) |
| F30 | `GET /api/v1/saloons/{id}/assignments/` | manager+ | none | `200`: explicit staff→saloon rows (makes the "assigned engineers" behind prescription-push explicit and auditable) |
| F31 | `POST /api/v1/saloons/{id}/assignments/` | manager+ | `{user_id (org member), scope: engineer\|worker\|doctor}` | `201`. `409` on duplicate active row |
| F32 | `DELETE /api/v1/assignments/{id}/` | manager+ | — | `200`: unassigned (history kept, `is_active=false`) |
| F33 | `GET /api/v1/saloons/{id}/flocks/{fid}/feed-deliveries/` | engineer+ | `limit`/`offset` | `200`: `{results:[{id, delivered_at, tonnes, supplier?, batch?}], count}` (receipts behind the net-consumption `feed_kg`) |
| F34 | `POST /api/v1/saloons/{id}/flocks/{fid}/feed-deliveries/` | engineer+ | `{delivered_at, tonnes (>0), supplier?, batch?, unit_price?}` | `201`: receipt row (FCR-auditable; priced rows feed future cost-per-kg) |
| F35 | `GET /api/v1/saloons/{id}/documents/` | member of owning org | `limit`/`offset` | `200`: `{results:[{id, title, file_ref, uploaded_by, at}], count}` (SOPs, certificates — beside photos) |
| F36 | `POST /api/v1/saloons/{id}/documents/` | engineer+ | `multipart`: `file` (pdf/jpg/png ≤20 MB), `title` | `201`: stored (same storage setting as photos) |

## 2. New modules (all new files)

`views_follow.py` / `serializers_follow.py` / `urls_follow.py` (F1–F36) ·
`services_follow.py` (delete-guards F3/F6, mute-overlap check, forward-only ticket machine,
turnaround catalog + auto-finish, search across three entities RLS-scoped).

## 3. Error contract (same envelope)

`401` · `403 {"error":"forbidden"}` · `404` (foreign-id oracle guard everywhere) ·
`400` bad window/overlap/backward jump/active-flock conflict/short query ·
`409` non-empty delete, duplicate label/assignment, past-window mute.

## 4. Explicit non-scope

- Sensor calibrations (GS-CALIB stays owner-deferred — deliberately absent).
- Equipment service log (Phase 2).
- Template writes, photo/document delete (retention undecided — keep until DR/legal decides).
- Calibration, billing, actuator dispatch (unchanged positions).

## 6. New tables (all additive, all `org_id`-scoped + RLS)

| Table | Key columns |
|---|---|
| `turnaround_checklists` | `id SERIAL PK`, `org_id UUID`, `saloon_id INT`, `flock_id INT NULL`, `started_at TIMESTAMPTZ`, `finished_at TIMESTAMPTZ NULL`, `created_by INT` |
| `turnaround_items` | `id SERIAL PK`, `checklist_id INT`, `task_key TEXT` (one of the 8-task catalog: `litter-out`, `wash`, `disinfect`, `downtime-wait`, `repair-check`, `preheat-test`, `waterline-flush`, `signoff`), `done BOOL DEFAULT FALSE`, `signed_by INT NULL`, `signed_at TIMESTAMPTZ NULL`, `UNIQUE(checklist_id, task_key)` |
| `staff_assignments` | `id SERIAL PK`, `org_id UUID`, `saloon_id INT`, `user_id INT`, `scope TEXT CHECK (engineer/worker/doctor)`, `is_active BOOL DEFAULT TRUE`, `created_at` |
| `feed_deliveries` | `id SERIAL PK`, `org_id UUID`, `saloon_id INT`, `flock_id INT`, `delivered_at TIMESTAMPTZ`, `tonnes NUMERIC CHECK (>0)`, `supplier TEXT NULL`, `batch TEXT NULL`, `unit_price NUMERIC NULL` (per-tonne price — §4r fix 2026-09-05: enables feed-cost-per-kg-gain without another table; NULL = price unknown) |
| `saloon_documents` | `id SERIAL PK`, `org_id UUID`, `saloon_id INT`, `title TEXT`, `file_ref TEXT`, `uploaded_by INT`, `created_at` |

## 7. Acceptance checklist (docs-side)

- [ ] F1–F36 matrix vs staging; delete-guards proven (non-empty farm/saloon refuse with 409).
- [ ] F24 issues a token whose `org_id` is the target (multi-org switch works end-to-end).
- [ ] Turnaround refuses during active flock; auto-finishes on 8th signature.
- [ ] yaml ratification commit re-greens `implemented == contract`.
