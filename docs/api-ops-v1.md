# Ops Read APIs — Endpoint Spec (Contract v1.7.0-draft, App v0.6 scope extension)

> **Status:** DRAFT 2026-09-05 · **Additive-only** — no `ALTER` on core tables, no change to
> existing `/api/v1` v1.5.0 paths. One small new table (`user_sessions`, §7); everything else
> reads frozen tables.
> **Canonical homes:** this file (v1.7.0 surface) · `API Contract (OpenAPI).yaml` v1.5.0 (frozen core,
> yaml edit reserved for the ratification commit) · decisions: `Open Items Tracker.md` §4p ·
> plan: `VERSIONS.md` §v0.6 · release draft: `docs/releases/v1.7-ops-draft.md` · history:
> `CHANGELOG.md [Planned → 1.7.0]`.
> Sibling specs: `docs/api-compat-flutter-v1.md` (v1.6.0) · `docs/api-growth-v1.md` (v1.8.0) ·
> `docs/data-aggregates-v1.md` (rollup tables).
>
> **v1.7.1 addendum (2026-09-12, owner decision):** the **threshold-template WRITE surface
> (O13–O16)** — additive to the v1.7.0 route table below and superseding the §5 exclusion.
> The contract yaml carries it at 1.7.0 → 1.7.1; the mirror was regenerated in the same wave.

---

## 1. Pagination standard (new convention, applies to ALL v1.7.0+ list endpoints)

Existing v1.5.0 list shapes stay byte-identical. Every NEW list endpoint follows this:

- Query: `limit` (default 50, max 500), `offset` (default 0). Both optional, non-negative ints.
- Response: `{"results": [...], "count": N}` where `count` is the total matching rows.
- Errors: negative/non-numeric → `400 {"error":"invalid pagination"}`.

## 2. Route table (all `BearerAuth`, all tenant-scoped via JWT `org_id` + RLS)

| # | Method + Route | Role | Request | Response |
|---|---|---|---|---|
| O1 | `GET /api/v1/audit-logs/` | owner, manager | `limit`/`offset`, `actor_id?`, `action?`, `farm_id?`, `saloon_id?`, `from?`, `to?` (ISO `Z`) | `200`: `{results:[{id, actor_id, username, action, details, created_at}], count}`. Source: `account_activity_logs` (read-only; rows stay write-only elsewhere) |
| O2 | `GET /api/v1/thresholds/` | engineer+ | `saloon_id?`, `metric_key?`, `limit`/`offset` | `200`: `{results:[{id, metric_key, template, day_from, day_to, min, max}], count}` — **ERRATUM 2026-09-06 (ratification)**: the frozen `lifecycle_threshold_rules` table carries day-ranged `min`/`max` bands per template, NOT per-saloon `tier1..tier3` (no such columns exist); the sketch below was written against a table shape that was never built. Source: `lifecycle_threshold_rules` (own-org templates + the global catalog) |
| O3 | `PUT /api/v1/thresholds/{id}/` | manager+ | `{min?, max?}` (partial ok; validated against metric sanity bounds) | `200`: updated rule + audit row (`account_activity_logs`, THRESHOLD_UPDATED). `400 {"error":"threshold out of range"}` on absurd values. **ERRATUM 2026-09-06**: write scope = OWN-org templates only — global (template.org NULL) catalog rows are readable via O2 but writable by no one (cross-tenant write hazard); no `enabled` column exists to toggle |
| O4 | `GET /api/v1/threshold-templates/` | engineer+ | `breed_type?` | `200`: `lifecycle_templates` rows (read-only catalog) |
| O5 | `GET /api/v1/saloons/{id}/photos/` | any member of owning org | `limit`/`offset` | `200`: `{results:[{id, url, caption, taken_at, uploaded_by}], count}`. Source: `saloon_photos` |
| O6 | `POST /api/v1/saloons/{id}/photos/` | engineer+ | `multipart`: `file` (jpg/png ≤10 MB), `caption?`, `taken_at?` | `201`: created row. `400` on type/size violation. Storage backend (S3/local) is a deploy setting, not part of this contract |
| O7 | `GET /api/v1/gateways/` | manager+ | `farm_id?`, `status?` (`PENDING_INSTALLATION`/`PENDING_APPROVAL`/`PROVISIONED`/`SUSPENDED`/`REJECTED`), `limit`/`offset` | `200`: `{results:[{slot_id, farm_id, provisioning_status, last_seen_at, saloon_count}], count}`. Read-only — state changes stay in `/provisioning/...` + `/gateway-requests/...` |
| O8 | `GET /api/v1/gateways/{slot}/` | manager+ | `slot` path | `200`: single gateway + its saloon zones + active hardware id. `404 {"error":"gateway not found"}` |
| O9 | `GET /api/v1/gateways/{slot}/diagnostics/` | engineer+ | `slot` path | `200`: last retained `T8/T9` diagnostics payload `{slot_id, reported_at, battery_v?, rssi?, free_heap?, sd_ok?, errors[]}` or `{"error":"no diagnostics yet"}` with `404` when the gateway never reported. Served from retained last-diagnostics records (new Redis `diag:last:{slot}` key written by the subscriber — additive, §7) |
| O10 | `POST /api/v1/auth/password/change/` | any authenticated user | `{current_password, new_password}` (new ≥10 chars, checked against common-password denylist) | `200`: ok. `400` on weak/new-equals-old, `401` on wrong current. Rate-limited harder: `3/min` per user (new `auth_strict` throttle, additive setting) |
| O11 | `GET /api/v1/auth/sessions/` | any authenticated user (own sessions only) | none | `200`: `[{id, created_at, last_seen_at, ip, user_agent, current}]`. Source: `user_sessions` (§7) |
| O12 | `DELETE /api/v1/auth/sessions/{id}/` | owner of the session | `id` path | `200`: revoked (that refresh line dies; access token inside 15 min expiry). `404` for foreign ids (never leak existence → same `404` either way) |
| O13 | `POST /api/v1/threshold-templates/` | owner, manager, engineer (**farm-scoped**) | `{name, breed_type?, farm_id?, rules:[{metric_key, day_from, day_to, min?, max?}]}` (`metric_key` ∈ temp/humidity/lux/nh3/co2/thi; `rules` ≤60) | `201`: created **DRAFT** + its bands + audit row (`THRESHOLD_TEMPLATE_CREATED`). `400` on unknown metric, bad day range, out-of-range value, unbounded `nh3` band or overlapping bands. `403` for an engineer with **no** `account_role_scopes` row (fail-closed) or outside the grant; `404` for a `farm_id` outside the org. A draft never arms a saloon |
| O14 | `PUT /api/v1/threshold-templates/{id}/` | same as O13 | partial `{name?, breed_type?, rules?}` — `rules`, when present, **replaces the whole band list** (bands are validated for overlap as a set) | `200`: updated DRAFT + audit row (`THRESHOLD_TEMPLATE_UPDATED`). `409` when the template is PUBLISHED (its bands are tuned through O3, which audits each change); `404` foreign / org-NULL built-in (identical) |
| O15 | `POST /api/v1/threshold-templates/{id}/publish/` | owner, manager | `{is_default?}` | `200`: PUBLISHED + `published_at`, **fires the alerts cache sweep** in the same request, audit row (`THRESHOLD_TEMPLATE_PUBLISHED`). `400` when the draft has no rules; `409` already published / the breed slot is taken / the org-default slot is taken; `404` foreign / org-NULL |
| O16 | `GET` / `PUT /api/v1/saloons/{id}/threshold-template/` | GET: any member of the owning org · PUT: owner, manager | PUT `{template_id}` — `null` CLEARS the assignment | `200`: `{saloon_id, assigned_template_id, effective_template_id, source}` with `source` ∈ `assigned`/`org_breed`/`org_default`/`global_default`/`none`; PUT re-caches that saloon immediately and writes `SALOON_TEMPLATE_ASSIGNED` / `_CLEARED`. `400` when the template is not published; `404` foreign/unknown saloon or template (never an existence oracle) |

## 3. New modules (all new files)

| Module | Contents |
|---|---|
| `views_ops.py` / `serializers_ops.py` / `urls_ops.py` (new) | O1–O12; `{"error": ...}` mapping; `limit`/`offset` validator shared with v1.8.0 |
| `services_ops.py` (new) | audit-log filter builder (RLS-scoped), threshold range guard, photo validator, diagnostics reader (`diag:last:{slot}`) |
| `user_sessions` writer (new, inside auth service) | insert row on login/refresh, touch `last_seen_at`, mark revoked on O12/logout-all |

## 4. Error contract (same envelope as v1.5.0)

`401` bad/missing token · `403 {"error":"forbidden"}` wrong role · `404` unknown id/slot ·
`400` bad pagination, bad threshold range, bad file · `429` past `5r/s` (O10: `3/min`).
Guessed foreign ids → `404`, never cross-org data, never existence oracle.

## 5. What v1.7.0 deliberately does NOT include

- No farm/saloon CRUD (Tracker §6 item stays parked for Phase-2 self-service).
- No tenant MFA enrollment (staff MFA lands v0.7; tenant MFA stays Phase 2).
- ~~No threshold-template writes (templates stay versioned/seeded; per-saloon rules are the tunable layer).~~ **SUPERSEDED 2026-09-12 by the v1.7.1 addendum (O13–O16):** the owner lifted the seeded-only rule. Templates gain DRAFT → PUBLISHED and per-saloon assignment; resolution stays deterministic (assignment → org+breed → org default → global default) and **only PUBLISHED rows arm alarms**, so authoring a draft cannot move a live band.
- No photo delete (retention policy undecided — keep until DR/legal decides; list + upload only).

## 6. Acceptance checklist (docs-side)

- [ ] O1–O12 answer against staging with exact keys above; `{results, count}` on every list.
- [ ] Role matrix green (worker `403` on O1/O3/O7; engineer `403` on O3-write; member ok on O5-read).
- [ ] O10 brute-force gate (`3/min`) tested; O12 foreign-id returns `404` identical to missing.
- [ ] `/api/v1 implemented == contract` gate re-greened after the yaml ratification commit.
- [x] O2/O3 rows corrected at ratification (2026-09-06): real-schema day-ranged min/max rows; write scope = own-org templates only; the §2 sketch's `tier1..tier3`/`enabled`/`updated_at` keys never had columns.
- [x] O13–O16 landed at contract v1.7.1 (2026-09-12): the yaml now carries the 4 paths through ONE pattern each (`GET`+`POST` on `/threshold-templates/`, `GET`+`PUT` on `/saloons/{id}/threshold-template/` — a second pattern for the same route would shadow a method and answer `405`), `implemented == contract` is green again (`tests/test_openapi_contract.py`), and the regenerated mirror passes `tests/test_openapi_schema.py`.
- [x] O13–O16 verified by the gate suite (2026-09-12): full CI on Postgres/Timescale + Redis **551 passed / 3 skipped @ 89.92%**, module coverage gate GREEN, migrate-gate RESULT: DEPLOYABLE (alerts `0005`, farms `0002`, tenants `0005`).
- [ ] O13–O16 exercised under enforced RLS on a non-superuser connection in a live drill: unscoped-engineer `403`, foreign farm `404`, and "a draft changes no hash" observed end-to-end.

## 7. New storage (ratified shapes)

- **`user_sessions`** (accounts 0003): id · user FK · refresh_jti (unique — the register key) · created_at / last_seen_at (db_default Now) · ip / user_agent · revoked_at. Written at login (and refreshed on refresh); O11 lists the caller's live rows (`current` derived from the access token's `sid` claim); O12 revocation sets `revoked_at` and RefreshView refuses that jti afterward (the token_blacklist app stays uninstalled — this table is the register). 404 identical for foreign/unknown session ids (existence-oracle guard).
- **`lifecycle_templates` write-surface columns** (alerts `0005`): `status` (DRAFT | PUBLISHED, default PUBLISHED so seeded rows and the org-NULL built-in catalog keep arming exactly as before), `farm` (NULL = org-wide; the engineer's scope anchor), `created_by` (the audit trail beside `account_activity_logs`), `published_at`; plus the two ambiguity guards `uq_lifecycle_templates_org_breed` and `uq_lifecycle_templates_org_default` — org-scoped AND PUBLISHED-only, so drafts stay unconstrained and a resolution is never left to row order.
- **`saloons.threshold_template`** (farms `0002`): nullable `RESTRICT` FK, the first link of the resolution chain. NULL = inherit the breed/default chain — byte-identical to the pre-column behaviour, so the migration is a data no-op; RESTRICT = a template is DEPRECATED, never deleted out from under the saloons and alert history that point at it. A separate assignment table was rejected deliberately (1:1 relationship; it would need its own `database.md` row and both RLS parity lists). Assignment history lives in `account_activity_logs` (`SALOON_TEMPLATE_ASSIGNED` / `_CLEARED`).
- **`lifecycle_templates_global_read`** (tenants `0005`): SELECT-only policy `TO morghdari_app USING (org_id IS NULL)`. Under ENFORCED RLS the org-NULL built-in catalog was invisible to the app role — `org_id = current_setting('app.current_org_id')` is never true for NULL — while dev/CI (superuser) saw it plainly. Harmless while templates were seeded-only; load-bearing now that the chain's last step is the global default and O4 must show the built-in rows. Twin block in `initdb/02_rls.sql`; `scripts/check_rls_parity.py` compares the table set, which this does not change.
- **`diag:last:{slot}`** (Redis hash, subscriber-written): reported_at (gateway device_ts verbatim) · received_at (unix) · battery_v / rssi / free_heap · sd_ok (1/0) · errors (JSON array). Written ONLY for diagnostics-only payloads (empty readings, Firmware §3.8) that previously died silently at Gate 5; O9 serves it with 404 when absent (Redis down = same honest 404).
