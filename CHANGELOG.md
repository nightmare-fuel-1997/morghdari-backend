# CHANGELOG

All notable changes per version. Format follows the spiral-roadmap discipline:
one section per version tag, each listing what risk the version killed.

## [v0.3] — 2026-08-26 — ingestion + fake gateway (M3)

The product's data path exists end to end: a simulated gateway publishes
schema-2.0 payloads, and the owner's own reading comes back through the API —
tenant-scoped, deduped, lane-separated, dead-lettered on poison.

### Risk killed
**Silent telemetry loss / absent ingestion path.** QoS-1 at-least-once is now
effectively-once (subscriber NX dedup + deterministic recv_ts + XACK after
commit); poison payloads land in `telemetry:dead_letter` WITH reasons; a 4-hour
outage replays through the historical lane without delaying live readings; both
streams are bounded by write-path trim.

### Added — M3 ingestion
- `backend/morghdari/` subscriber package: `$share/telemetry_workers/` shared
  subscription, qos=1, persistent session, REQUIRED unique client id
- Pipeline gates: strict topic parse → payload identity cross-check → schema
  version → device_ts presence → per-slot dedup (`SET NX EX 90000`) → slot
  resolution (24h cache / DB fallback / UNKNOWN_SLOT DLQ) → enrich+XADD per
  reading into realtime/historical lanes → approximate trim (~100k) every write
- Celery app + consumers: XACK strictly after DB commit, per-org
  atomic+tenant_context batches, own-pending crash recovery,
  `bulk_create(ignore_conflicts=True)` over deterministic recv_ts
- Historical pacing from system_configuration EVERY cycle (50/min global,
  20/org sliding window) — config-is-data, no redeploy
- Full simulator: multi-pack schema-2.0 payloads, `--replay-hours N` storm then
  live resume, compressed demo cadence
- `GET /saloons/{id}/telemetry/`: RLS-scoped read with since/until/slot_id/
  include_buffered filters; DB-generated `buffered_delay_mins` via annotate

### Decisions & constraints
Q1–Q6 approved (Tracker §4k): non-PROVISIONED → DLQ reason; consumer group
`telemetry_workers`; CI broker-less (in-process stubs vs real redis/postgres);
flat stream envelope incl recv_ts+trace_id; historical route added before the
wildcard; metric auto-discovery parked. **C-15 stamped:** RLS assertions are
VACUOUS under superuser connections — request-phase tests must
`SET ROLE morghdari_app`.

### Verification
- Isolated CI gate: **130 passed @ 87.37%** on timescale/timescaledb:2.29.2-pg16
- Storm integration (local PG+Redis): 240-window backlog + 20 live stay
  lane-separated; FULL identical replay yields 60 rows not 120; hypertable
  chunking verified via timescaledb_information
- Demo transcript: `versions/v0.3/DEMO-M3.md`; look-back answers in
  `versions/v0.3/README.md`

### Real-world constraints surfaced
Headliners: monotonic recv_ts requirement (Windows clock-tick collisions
silently dropped rows via UNIQUE+ignore_conflicts); superuser BYPASSRLS vacuous
test discipline (C-15); Django<5.2 generated-column exclusion from unmanaged
models; Redis scratch-DB isolation for integration tests.

## [v0.2] — 2026-08-25 — auth & tenancy (M2)

Identity and role enforcement on top of the RLS foundation. Contract bumps
v1.4.3 (org-member management) and v1.4.4 (support-session access_token erratum);
decisions D1–D7 owner-approved and recorded in backend_docs Tracker §4j;
constraint **C-14 pre-registered**: deactivated-member reads return HTTP 200
with EMPTY payloads by design — do not "fix".

### Risk killed
**App-layer isolation & role enforcement.** Every authenticated request now
carries validated JWT claims; the tenant GUC is set inside ONE transaction per
request by `TenantOrgMiddleware` (branch order ①–④, signature verified exactly
once); RBAC lives in a single choke point (`require_role`/`RequireRole`) with
account_role_scopes narrowing. Deactivation revokes tenant authority without
any blacklist: live tokens die ≤15 min, next refresh is org-less, reads fail
closed-empty. Accepted risk (Security §3.2, unchanged): a leaked refresh token
stays REUSABLE to its natural 7-day expiry.

### Added — M2 auth & tenancy
- SIMPLE_JWT settings (15-min access / 7-day refresh; rotation+blacklist OFF
  forever, Tracker §4i); resolved pin 5.5.1 recorded; no token_blacklist app
- `/auth/login/` (contract envelope `{access_token, refresh_token, user{}}`) +
  stock `/auth/token/refresh/` (native `{access}` — asymmetry contractual)
- Claims at birth: oldest-active-org pick (zero→401), sorted role union,
  platform-staff flag; STAFF LOGIN IS ORG-LESS (D15 axis, docs erratum-2)
- `CachedJWTAuthentication` verify-once stash + `TenantOrgMiddleware`
  (support-session claim beats staff-skip; whole view inside one atomic)
- Org-member management per contract v1.4.3: list / invite (unusable password,
  201; reactivation of inactive row 200; duplicate active 409) / deactivate
  (soft-off, never delete) — owner-exclusive writes
- `POST /platform/support/session/`: staff-not-superuser, mandatory incident_id,
  900s scoped token, SUPPORT_ORG_ENTER audit written before success
- Offboarding chain proven end-to-end incl. C-12 residue on pooled connections

### Verification
- Isolated CI gate: **91 passed @ 87.25%** on timescale/timescaledb:2.29.2-pg16
- Full local postgres mode: 74 passed pre-matrix, zero skips; fresh dev volume
  unaffected (C-13 `-p morghdari_ci` discipline held all round)
- Demo transcript: `versions/v0.2/DEMO-M2.md`; look-back answers recorded in
  `versions/v0.2/README.md`

## Post-v0.1 retrospective resolutions — 2026-08-24 (applied before v0.2 build)

All three open decisions from `versions/v0.1/RETROSPECTIVE.md` were APPROVED and applied:

- **C-11 RESOLVED (keep security):** `saloon_telemetry` permanently keeps RLS with NO
  columnar compression for v0.x; 180-day retention unchanged. Docs' capacity tables and
  lifecycle sections updated to assume uncompressed storage.
- **C-08 RESOLVED (drop FK):** database.md removes the un-executable `REFERENCES
  gateway_hardware_registry(hardware_id)` clause; integrity enforced by the provisioning
  sync writer at app level.
- **C-10 RESOLVED (org_id everywhere):** `alert_acknowledgements` gains `org_id UUID NOT NULL`
  doc-first; repo model + AddField/backfill/AlterField migration + both RLS policy lists synced.
  Org-scoped RLS universe: 35 → **36** tables, parity-checked 1:1 across doc/initdb/migration.
- Policy template NULLIF guard mirrored through Security Architecture §4.1, System Walkthrough
  and Saas.md; docs ci compose gains `name: morghdari_ci`; EMQX mechanism-pair verified present.

## [v0.1] — 2026-08-24 — walking skeleton + full schema + RLS

The smallest honest Morghdari: a Django project that boots in Docker, a CI
pipeline running against real TimescaleDB, and the ENTIRE ~50-table schema
under Row-Level Security. No auth, no endpoints, no business logic (v0.2+).

### Risk killed
**Tenant isolation at the database layer.** From this version forward no code
path can be written that leaks one tenant's rows to another without actively
defeating FORCE ROW LEVEL SECURITY in SQL.

### Added — M0 walking skeleton
- Multi-stage non-root Dockerfile (`builder`/`test`/`runtime`, §8) + `.dockerignore`
- `docker-compose.dev.yml` (timescale 2.29.2-pg16 / redis / emqx 5.5) and
  `docker-compose.ci.yml` — CI gate: `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build test`
- Four-module settings split; DATABASE_URL/REDIS_URL single-env convention;
  production fails loudly without secrets
- 12 Blueprint §1 app packages with dotted AppConfig names
- `.env.example`; `initdb/00_extensions.sql`

### Added — M1 schema/roles/RLS
- All 45 database.md tables transcribed verbatim across apps
  (tenants/accounts/geography/farms/devices/telemetry/alerts/system_config/
  provisioning/auditing/mqtt_auth) incl. D22/D23 production records,
  maintenance_mutes, legal_hold columns, tier quotas, metric catalog
- TimescaleDB hypertables `saloon_telemetry` (+ org space dimension) and
  `metric_hourly_aggregates` via RunSQL migration; 180-day retention policy
- Five DB roles (owner/app/admin-BYPASSRLS/emqx_auth/exporter) via
  `initdb/01_roles.sql`
- ENABLE+FORCE RLS + `tenant_isolation` over all 35 org-scoped tables
  (`initdb/02_rls.sql` + frozen migration), org-less globals deliberately free
- Seeds: 8 metric keys, 26 system_configuration keys, 3 tier quotas,
  build-provided geo sample — idempotent
- `apps/tenants/context.py` tenant_context(); `tests/test_rls.py` exit gate;
  `scripts/check_rls_parity.py`; `scripts/simulate_gateway.py` scaffold

### Real-world constraints surfaced (CONSTRAINTS.md C-01..C-13)
Headliners: C-11 TimescaleDB compression ⊕ RLS mutual exclusion (decided 2026-08-24 —
see [Unreleased]); C-12 pooled-session GUC '' quirk forcing the
NULLIF-hardened policy template; C-07/C-08/C-09 doc-vs-Django seams
(composite PK, non-unique FK target, custom user model).

### Verification
- CI gate: 14 passed @ 84.6% coverage on timescale/timescaledb:2.29.2-pg16
- Fresh-volume bootstrap: 58 migrations clean; 35/35 org tables FORCE'd
- Fail-closed demo recorded in `versions/v0.1/DEMO-M1.md`

### Known gaps parked for owner retrospective
- Farm/saloon CRUD endpoints decision (Tracker §6)
- KB site resync with docs changes (§7 edits not yet reflected)
- alert_acknowledgements org_id (C-10) — APPROVED & APPLIED 2026-08-24, see [Unreleased]
