# CHANGELOG

All notable changes per version. Format follows the spiral-roadmap discipline:
one section per version tag, each listing what risk the version killed.

## [demo live dashboard] — 2026-08-30 — fake gateways, real charts (demo branch)

Fake-but-realistic gateways and sensor packs publish schema-2.0 payloads over
the REAL MQTT path into the REAL pipeline, and a new `/demo/dashboard/` page
renders live SVG charts, per-slot liveness LEDs, an alert strip and pipeline
stats — so a showroom proves the whole stack with moving numbers instead of
static screenshots.

### Added
- `manage.py demo_farm_seed`: idempotent demo topology (org, owner, 3 saloons ×
  2 packs with slot mappings), global default lifecycle template + day-band
  threshold rules, open flock per saloon, threshold hashes recached; demo
  pacing `breach_duration_sec=30` (visible Tier-1) with `--restore` back to 900
- `scripts/simulate_farm.py`: one MQTT client per fake device; realistic
  diurnal temp sine + NH3 drift + humidity; scripted sustained NH3 breaches and
  instant velocity spikes (`max_ts >= 50`), then recovery; `--dry-run`,
  `--scenario calm`
- `GET /demo/api/farm/` (JWT): caller-org topology + per-slot liveness from the
  SAME `sensor:status:*` keys the LOS watchdog uses + latest real reading
- `/demo/dashboard/` page: KPI strip, per-saloon temp/NH3 SVG charts with
  threshold lines, pack tiles with LED liveness, live alert strip, pipeline
  stats; EN/FA toggle; 5 s polling paused on hidden tabs
- `backend/demo_live.bat`: one-click launcher (stack → migrate → seed →
  subscriber/worker/beat/Django → feed)

## [v0.4] — 2026-08-29 — alert engine (M4)

The product's safety brain exists end to end: threshold cache, breach state
machine, 3-tier escalation, LOS watchdog, replay sweep, correlation rule and
quality metrics — Redis state machines with exactly-once semantics.

### Risk killed
**Silent alerting failure / false alarms.** Sustained breaches fire exactly-once
at 900 s; escalation promotes Tier 2/3 with ack-pause freeze; LOS is detected
within 60 s + 300 s grace; replay storms never page anyone; a missing sensor is
UNAVAILABLE, never assumed normal.

### Added — M4 alert engine
- Threshold cache per saloon/day: midnight cron + flock-change signal re-cache;
  end_date mid-day re-arms; 4 seeded tunables (breach 900s, status TTL 300s,
  tiers 15/30) — M4-01
- Breach accumulation with TTL-refresh + hysteresis (+0.5) + midnight grace 300 s;
  NH3 velocity bypass (config key, max_ts >= 50 -> instant Tier 1, skips
  grace/mute/suppression entirely) — M4-02/03
- alert_instances writes: inline evaluation after telemetry commit / before ack;
  REALTIME row stamps last breach value (M4-04)
- Escalation sweep + 3-tier timeline: breach key holds JSON state so Tier 1 fires
  EXACTLY ONCE at 900 s; 2-min sweep promotes Tier 2 @ 15 min / Tier 3 @ 30 min,
  never demotes, ack-pause freezes the timer; delivery stubs per tier; beat entry
  `alerts-escalate-alerts` every 120 s — M4-05
- Ack mechanism: acknowledgement row + pause key written TOGETHER inside one
  atomic block (Redis SET pre-commit so redis-down rolls back the row); pause TTL
  from `alert_acknowledgement_pause_minutes` every call (Audit-M5 clause);
  re-ack adds another audit row and refreshes the window — REST endpoint itself
  is v0.6/M6-01 (M4-06)
- LOS watchdog: 60 s beat entry `alerts-los-watchdog`; the 300 s grace IS the
  `sensor:status:{saloon}:{slot}` TTL written by the consumer on every LIVE
  reading (buffered/replayed readings never refresh it — a replay storm must not
  mask a real outage); exactly ONE saloon-scoped Tier-1 row per outage episode
  via setnx `los:fired:{saloon}:{slot}` + unresolved-row backstop, routed
  installer+hardware_team; recovery owned by the watchdog (`escalate_all`
  excludes `metric_type='los'`); DR-T3 suppression: seeded
  `los_suppress_after_redis_restart_min`=10 (migration 0005 + conftest
  SEED_MIGRATIONS replay), sweep silent while Redis uptime < window — M4-07
- Replay sweep: drained backlog => retroactive REPLAY_ANALYSIS rows with
  `batch_id` = `{slot}:{first_device_ts}` (idempotent re-runs; a
  `replay:covered:{slot}` high-water mark lets a LATER outage open a fresh
  window), worst breaching value stamped, NEVER pushed; dispatch per Blueprint
  §5 — historical drain counts `replay:pending:{gateway_slot}` down once per
  unique entry, at <= 0 -> `alerts.run_replay_alert_sweep` exactly once via
  `replay:sweeping` NX, marker deleted in `finally` so the next outage re-arms —
  M4-08
- D23 capability-gated rules: THI heat-stress DERIVED in-engine from co-present
  temp+humidity averages (NRC formula `THI=(1.8T+32)-(0.55-0.0055*RH)(1.8T-26)`
  — corpus pins the rule but not the constant, recorded for retrospective),
  evaluated through the normal breach/duration machinery against `thi_max` from
  the hash; missing input => THI UNAVAILABLE (never derived from one input, no
  phantom breach state); `co`/`power_*` rules ride the normal `{metric}_max`
  hash path and evaluate ONLY when an entry carries the key — a saloon without
  the sensor stays structurally inert (missing metric = unavailable, never
  assumed normal); canonical `test_capability_profile` deferred to M6 (Tracker
  §4l-Q5) — M4-09
- GS-HUMAN-SAFETY REJECTED (owner, 2026-08-23 — Tracker §4i): no flock-independent
  baselines; alerting stays flock-gated (D10 velocity bypass unchanged). Lands
  instead: GS-CORRELATION farm-wide LOS correlation — >= 0.7 of a farm's ACTIVE
  slots LOS within 3 min => ONE consolidated page via `farm:los:{farm_id}`
  sliding ZSET + NX `farm:corr:muted:{farm_id}` (TTL =
  `correlation_suppress_minutes`); per-sensor rows still log/alert-instance, zero
  schema change, suppressed farms log INFO not WARNING; GS-MAINT-MUTE —
  `maintenance_mutes` windows silence threshold/duration alerting (evaluation
  step before breach/duration), velocity always bypasses (Decision 10),
  suppressed-count kept in Redis per mute + the escalation sweep writes ONE
  structured digest line per ended window (NX-guarded) — M4-10
- MET-ALERT-QUALITY export (Observability §5.2, Tracker §4l-Q8): implemented as
  exportable functions so the v0.4 retrospective and (later, M7) Grafana render
  consume the SAME definitions — `mttr_seconds_by_tier` (mean resolved_at -
  triggered_at per tier), `ack_to_resolve_seconds` (mean resolution for acked vs
  never-acked, row truth from the ack table), `alerts_per_flock` (REALTIME
  alerts + mortality % per flock cycle), `snapshot()` stitching all three;
  Django 5.0 removed ExtractEpoch — backend-agnostic replacement fetches
  timestamp pairs and averages durations in Python (exact on Postgres prod and
  SQLite test fallback) — M4-11

### Verification
- Full local Postgres mode: **215 passed @ 88.64%** total coverage;
  `apps.alerts.services` all >= 84%: breach 84%, firing 79%, ack 93%, mutes 97%,
  sweeps 92%, thresholds 92%, velocity 91%, replay 91%, quality 98%
- SQLite fallback: 117 passed (98 Postgres-required tests skipped)
- M4-11 acceptance: 9/9 quality tests green; coverage >= 90% on
  `apps.alerts.services.quality` (98%)

### Real-world constraints surfaced
- Django 5.0 removed ExtractEpoch: backend-agnostic replacement fetches
  timestamp pairs and averages durations in Python (seconds) — exact on both
  the Postgres prod path and the SQLite test fallback (no native duration
  extraction on sqlite)
- `account_activity_logs` NOT-NULL actor FK: system-authored digest rows need an
  actor decision — friction recorded for the M4-12 retrospective
- NRC THI formula: corpus pins the rule but not the constant — recorded for the
  M4-12 retrospective

### Decisions & constraints
Q1–Q10 approved (backend_docs Tracker §4l, all approved 2026-08-26): contract
bumped to v1.4.5. GS-HUMAN-SAFETY rejected by owner (Tracker §4i) — alerting
stays flock-gated (D10 velocity bypass unchanged).

## [v0.3] — 2026-08-26 — ingestion + fake gateway (M3) — 2026-08-26 — ingestion + fake gateway (M3)

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
