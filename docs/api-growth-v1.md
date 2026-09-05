# Growth & Integration APIs — Endpoint Spec (Contract v1.8.0-draft, App v1.1 scope extension)

> **Status:** DRAFT 2026-09-05 · **Additive-only** — no `ALTER` on core tables. Four small new
> tables (`push_tokens`, `notification_preferences`, `webhook_endpoints`/`webhook_deliveries`,
> `import_jobs`, `export_jobs`, §7); everything else reads frozen tables.
> **Canonical homes:** this file (v1.8.0 surface) · decisions: `Open Items Tracker.md` §4p ·
> plan: `VERSIONS.md` §v1.1 · release draft: `docs/releases/v1.8-growth-draft.md` · history:
> `CHANGELOG.md [Planned → 1.8.0]`.
> Sibling specs: `docs/api-ops-v1.md` (v1.7.0) · `docs/data-aggregates-v1.md` (rollup tables).
> Pagination on all new lists follows the v1.7.0 convention (`limit`/`offset` →
> `{results, count}`).

---

## 1. Route table (all `BearerAuth`, tenant-scoped via JWT `org_id` + RLS)

| # | Method + Route | Role | Request | Response |
|---|---|---|---|---|
| G1 | `POST /api/v1/devices/push-tokens/` | any member (own tokens) | `{token, platform: fcm\|apn, device_label?}` | `201`: registered (idempotent on same token → `200`). Serves GS-ROUTING urgency: without this, pilot alerts cannot reach phones |
| G2 | `GET /api/v1/devices/push-tokens/` | owner of tokens | none | `200`: own tokens `{results:[{id, platform, device_label, created_at}], count}` |
| G3 | `DELETE /api/v1/devices/push-tokens/{id}/` | owner of token | `id` path | `200`: revoked. Foreign ids → identical `404` |
| G4 | `GET /api/v1/me/notification-preferences/` | any member (own row) | none | `200`: `{quiet_from, quiet_to, tz}` (default: no quiet hours). Source: `notification_preferences` |
| G5 | `PUT /api/v1/me/notification-preferences/` | any member (own row) | `{quiet_from?, quiet_to?}` (`HH:MM` Asia/Tehran), `tz?` | `200`: saved. Quiet hours suppress push/SMS only — Tier-1 velocity still pages per Decision 10 (same rule as category mutes, §4n D24-6) |
| G6 | `GET /api/v1/webhooks/` | manager+ | `limit`/`offset` | `200`: `{results:[{id, url, events[], is_active, created_at}], count}` |
| G7 | `POST /api/v1/webhooks/` | manager+ | `{url (https only), events[] ⊂ {alert.triggered, alert.resolved, report.flagged, flock.started, flock.ended}, secret?}` | `201`: endpoint + generated signing secret (shown once). `400` on http URLs or unknown events |
| G8 | `DELETE /api/v1/webhooks/{id}/` | manager+ | `id` path | `200`: deleted (deliveries retained 30 d for debugging, then purged) |
| G9 | `GET /api/v1/webhooks/deliveries/` | manager+ | `webhook_id?`, `status?` (`delivered`/`failed`/`pending`), `limit`/`offset` | `200`: `{results:[{id, webhook_id, event, status, attempts, last_error, created_at}], count}`. Delivery: at-least-once with backoff (5 tries), `HMAC-SHA256` signature header, per-endpoint `5/min` cap |
| G10 | `POST /api/v1/imports/{kind}/` | manager+ (`members` → owner-only) | `kind ∈ {members, flocks, thresholds}`, `multipart file` (CSV ≤5 MB, header validated) | `202`: `{job_id, status: QUEUED}` — async Celery validation + apply; row-level errors collected, never partial apply without `--dry-run` report first. `GET /api/v1/imports/{job_id}/` polls `{status, total_rows, ok_rows, errors[]}` |
| G11 | `GET /api/v1/imports/{job_id}/` | same as G10 caller org | `job_id` path | `200`: job status + per-row errors (first 100). `404` for foreign orgs |
| G12 | `POST /api/v1/exports/evidence-pack/` | manager+, doctor (vet inspections) | `{flock_id, from, to}` (ISO dates) | `202`: `{job_id}` — async pack: mortality × alert windows × biosecurity events × thresholds (feeds GS-EVIDENCE: vet inspections, insurance). `GET /api/v1/exports/{job_id}/` polls → `{status, download_url? (15-min signed), expires_at?}` |
| G13 | `GET /api/v1/exports/{job_id}/` | same as G12 caller org | `job_id` path | `200`: status + signed URL when ready. Packs retained 7 d, then purged (DR/legal to revisit) |

## 2. New tables (all additive, all `org_id`-scoped + RLS like every tenant table)

| Table | Key columns |
|---|---|
| `push_tokens` | `id SERIAL PK`, `org_id UUID`, `user_id INT`, `token TEXT UNIQUE`, `platform TEXT CHECK (fcm/apn)`, `device_label TEXT`, `created_at` |
| `notification_preferences` | `user_id INT PK`, `org_id UUID`, `quiet_from TIME NULL`, `quiet_to TIME NULL`, `tz TEXT DEFAULT 'Asia/Tehran'` |
| `webhook_endpoints` | `id SERIAL PK`, `org_id UUID`, `url TEXT`, `events TEXT[]`, `secret_hash TEXT`, `is_active BOOL DEFAULT TRUE`, `created_by INT`, `created_at` |
| `webhook_deliveries` | `id BIGSERIAL PK`, `org_id UUID`, `webhook_id INT`, `event TEXT`, `status TEXT`, `attempts INT DEFAULT 0`, `last_error TEXT`, `created_at`; 30-d retention job |
| `import_jobs` | `id SERIAL PK`, `org_id UUID`, `kind TEXT`, `status TEXT`, `total_rows/ok_rows INT`, `errors JSONB`, `created_by INT`, `created_at` |
| `export_jobs` | `id SERIAL PK`, `org_id UUID`, `kind TEXT DEFAULT 'evidence-pack'`, `params JSONB`, `status TEXT`, `file_ref TEXT NULL`, `created_by INT`, `created_at`; 7-d purge |

## 3. New modules (all new files)

`views_growth.py` / `serializers_growth.py` / `urls_growth.py` (G1–G13) ·
`services_growth.py` (CSV validators per kind, evidence-pack assembler from frozen tables,
webhook dispatcher with HMAC + backoff) · Celery tasks `webhooks.dispatch`,
`imports.apply`, `exports.build_pack` (all idempotent, retry-safe).

## 4. Error contract (same envelope)

`401` · `403 {"error":"forbidden"}` · `404` (incl. foreign-id oracle guard) ·
`400` bad CSV header / http webhook URL / unknown event / bad `HH:MM` ·
`429` past caps · `202` for every async accept (never `201` on a job).

## 5. Explicit non-scope

- No billing endpoints (D16 stays deferred; `org_daily_usage` in `docs/data-aggregates-v1.md` only meters).
- No silo/layer/vision modules (Phase 2 per DOM-*, Tracker §4h).
- No public status API (MET-STATUS lands v0.9 pilot as a static page, not a tenant endpoint).
- No actuator dispatch (501 stubs stay until Phase 2, D20).

## 6. Acceptance checklist (docs-side)

- [ ] G1–G5 green incl. quiet-hours + velocity-bypass test (Tier-1 pages during quiet hours).
- [ ] Webhook HMAC + retry + per-endpoint cap tested; deliveries page shows failures.
- [ ] Import dry-run catches row errors with zero writes; evidence pack contains the four sections.
- [ ] yaml ratification commit re-greens `implemented == contract`.
