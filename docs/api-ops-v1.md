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
| O2 | `GET /api/v1/thresholds/` | engineer+ | `saloon_id?`, `metric_key?`, `limit`/`offset` | `200`: `{results:[{id, saloon_id, metric_key, tier1..tier3, enabled, updated_at}], count}`. Source: `lifecycle_threshold_rules` |
| O3 | `PUT /api/v1/thresholds/{id}/` | manager+ | `{tier1?, tier2?, tier3?, enabled?}` (partial ok; values validated against metric sanity bounds) | `200`: updated rule + writes `provisioning_audit_log`-style audit row. `400 {"error":"threshold out of range"}` on absurd values (e.g. negative NH₃). Human-safety framing stays dropped per owner decision — this edits flock-gated rules only |
| O4 | `GET /api/v1/threshold-templates/` | engineer+ | `breed_type?` | `200`: `lifecycle_templates` rows (read-only catalog) |
| O5 | `GET /api/v1/saloons/{id}/photos/` | any member of owning org | `limit`/`offset` | `200`: `{results:[{id, url, caption, taken_at, uploaded_by}], count}`. Source: `saloon_photos` |
| O6 | `POST /api/v1/saloons/{id}/photos/` | engineer+ | `multipart`: `file` (jpg/png ≤10 MB), `caption?`, `taken_at?` | `201`: created row. `400` on type/size violation. Storage backend (S3/local) is a deploy setting, not part of this contract |
| O7 | `GET /api/v1/gateways/` | manager+ | `farm_id?`, `status?` (`PENDING_INSTALLATION`/`PENDING_APPROVAL`/`PROVISIONED`/`SUSPENDED`/`REJECTED`), `limit`/`offset` | `200`: `{results:[{slot_id, farm_id, provisioning_status, last_seen_at, saloon_count}], count}`. Read-only — state changes stay in `/provisioning/...` + `/gateway-requests/...` |
| O8 | `GET /api/v1/gateways/{slot}/` | manager+ | `slot` path | `200`: single gateway + its saloon zones + active hardware id. `404 {"error":"gateway not found"}` |
| O9 | `GET /api/v1/gateways/{slot}/diagnostics/` | engineer+ | `slot` path | `200`: last retained `T8/T9` diagnostics payload `{slot_id, reported_at, battery_v?, rssi?, free_heap?, sd_ok?, errors[]}` or `{"error":"no diagnostics yet"}` with `404` when the gateway never reported. Served from retained last-diagnostics records (new Redis `diag:last:{slot}` key written by the subscriber — additive, §7) |
| O10 | `POST /api/v1/auth/password/change/` | any authenticated user | `{current_password, new_password}` (new ≥10 chars, checked against common-password denylist) | `200`: ok. `400` on weak/new-equals-old, `401` on wrong current. Rate-limited harder: `3/min` per user (new `auth_strict` throttle, additive setting) |
| O11 | `GET /api/v1/auth/sessions/` | any authenticated user (own sessions only) | none | `200`: `[{id, created_at, last_seen_at, ip, user_agent, current}]`. Source: `user_sessions` (§7) |
| O12 | `DELETE /api/v1/auth/sessions/{id}/` | owner of the session | `id` path | `200`: revoked (that refresh line dies; access token inside 15 min expiry). `404` for foreign ids (never leak existence → same `404` either way) |

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
- No threshold-template writes (templates stay versioned/seeded; per-saloon rules are the tunable layer).
- No photo delete (retention policy undecided — keep until DR/legal decides; list + upload only).

## 6. Acceptance checklist (docs-side)

- [ ] O1–O12 answer against staging with exact keys above; `{results, count}` on every list.
- [ ] Role matrix green (worker `403` on O1/O3/O7; engineer `403` on O3-write; member ok on O5-read).
- [ ] O10 brute-force gate (`3/min`) tested; O12 foreign-id returns `404` identical to missing.
- [ ] `/api/v1 implemented == contract` gate re-greened after the yaml ratification commit.
