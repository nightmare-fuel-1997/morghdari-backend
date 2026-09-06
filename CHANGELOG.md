# CHANGELOG

All notable changes per version. Format follows the spiral-roadmap discipline:
one section per version tag, each listing what risk the version killed.

## [v0.6] — 2026-09-06 — full REST + the living dashboard (M6)

The dashboard is live (WebSocket push + reconnect gap-fill), the whole v1.1–v1.4
REST surface exists, and the shipped Flutter build runs against the backend
unchanged through the compat adapter. Contract gate: `implemented == contract`.

### Risk killed
**The live UX contract.** Before v0.6 the dashboard polled; now every committed
reading is pushed to every member's tab within the flush window ("UI follows
DB"), a dropped connection back-fills exactly what it missed, KPIs are computed
per request with honest NULLs, capabilities report what a saloon can and cannot
do (and how to enable it), and an old mobile build still works — unmodified —
through an additive compat layer. The OpenAPI diff gate holds the whole surface
to the contract: implementation drift breaks CI.

### Added — M6-01 operability (v1.1 endpoint set complete)
- 15 new endpoints over the 8 paths v0.2/v0.3/v0.5 already built: saloons
  detail/alerts/analytics-hourly, farms overview, provisioning reads
  (location-tree/provinces/cities/villages/positions), ack acknowledge,
  operational-state, flocks (incl. `placement_count` body), metric-types,
  health extended, hardware/time — completing the 32-path v1.1 surface

### Added — M6-02..M6-05 KPIs + capabilities (v1.3 + v1.4 + services)
- **M6-02** v1.3 endpoints: weight-samples GET/POST, feed-intake GET/POST
  (per-day upsert + audited corrections), saloons kpis, personal
  dashboard-config GET/PUT, role dashboard-config (owner-gated)
- **M6-03** v1.4 endpoints: water-intake GET/POST (feed twin),
  biosecurity-events GET/POST, saloon capabilities GET
- **M6-04** `apps/alerts/services/kpi.py` — the ONE home of the D22 math:
  every function returns `(value, reason)` tuples (D22+D23 as types),
  water:feed ratio (industry band 1.8–2:1, NULL until BOTH logs exist),
  compliance band helpers moved in so alerting and compliance share one
  source of truth (the view delegates)
- **M6-05** `apps/alerts/services/capability_profile.py` — FEATURE_MATRIX
  (declared requirements; `Requires.computed` carries computed availability
  facts like threshold bands + hourly data); the unavailable→available demo
  is proven in-suite (THI flips on a seen humidity key; compliance flips on
  bands + one hourly rollup row — the rollup table, not raw telemetry)

### Added — M6-06/M6-07 the living dashboard (WebSocket)
- **M6-06** `SaloonTelemetryConsumer` (Channels): one socket per dashboard tab
  on `WS /ws/saloons/{saloon_id}/telemetry?token=<JWT>` — verdicts in normative
  order (auth BEFORE cap, so a bad token never consumes a slot): no/invalid
  token or wrong org → close **4003**, org member at the D21
  `ws_max_connections_per_user` cap (Redis Set + 300 s zombie-guard TTL,
  refreshed on every inbound message) → close **4000**; register→re-check race
  guarded; drain fans `telemetry.update` out after each per-org commit
  (groups `saloon_{org_id}_{saloon_id}`; `is_buffered` rides the event)
- **M6-07** reconnect gap-fill: `{"action": "gap_fill", "since": ...}` →
  ordered `telemetry.gap_fill` from the hypertable inside the claim's tenant
  context; optional `slot_id` filter; `GAP_FILL_MAX_ROWS=200` ceiling with a
  friendly bad-`since` envelope; the server holds no session state
- ASGI wiring: `config/asgi.py` ProtocolTypeRouter + `config/routing.py`

### Added — M6-09 Flutter compat adapter (contract v1.6.0-draft, Tracker §4o)
Additive-only — new `apps/compat/*_compat.py` modules, one new table, zero
ALTER on core tables, zero change to the `/api/v1` paths:
- `legacy_slug_map` (unmanaged) + idempotent `backfill_legacy_slugs` (stable
  suggested slugs; second run creates zero rows)
- 15 REST routes under `/api/compat/v1` + legacy `/api/...` aliases with the
  `{"error": ...}` contract (foreign/bad slug → 404, inverted window → 400)
- flat telemetry translation with the exact Flutter keys
  (`temperature`/`humidity`/`lux`/`nh3`, `{"farm_history": [...]}` default 24)
- `FarmTelemetryConsumer` shim on `WS /ws/farms/{farmSlug}/telemetry` —
  subscribes to the core saloon groups, translates `telemetry.update`, never
  pushes buffered rows, same cap + 4003/4000 closes
- spec: `docs/api-compat-flutter-v1.md` · draft: `docs/releases/v1.6-compat-draft.md`

### Added — M6-10 ops read APIs (contract v1.7.0, Tracker §4p)
Additive modules in `apps/monitoring` (no new app — M0-05); pagination
convention `limit`/`offset` → `{results, count}` (shared-ready for v1.8.0):
- **O1** audit-log query (owner/manager; filterable; bad scalars → 400)
- **O2/O3** thresholds read (engineer+) / update (manager+, own-org templates
  ONLY — the global catalog is readable by all, writable by no one;
  THRESHOLD_UPDATED audit row); **O4** template catalog. Where the spec's
  sketch diverged from the frozen schema (day-ranged min/max, no
  `enabled`/`updated_at` columns), the API follows the real table and the
  spec carries the erratum
- **O5/O6** saloon photos GET (any member) / POST (engineer+, jpg/png ≤ 10 MB)
- **O7/O8** gateway + slot reads; **O9** `diag:last:{slot}` diagnostics reader
  (404 "no diagnostics yet" = honest D22 degradation) backed by the new
  subscriber hook: diagnostics-only payloads (empty readings, Firmware §3.8)
  are retained instead of dying silently at Gate 5 — telemetry path untouched
- **O10** password change with the `auth_strict` throttle (3/min per user — the
  ONE throttle lane; no default throttle added)
- **O11/O12** sessions: new `user_sessions` table (accounts 0003, jti-keyed,
  one row per refresh token) written best-effort at login/refresh; revoke
  kills that refresh line on its next use; `sid` claim rides the access token
  so the list marks the current line; blacklist app stays uninstalled
- ratification: `openapi.yaml` bumped 1.5.0 → 1.7.0 (+11 paths) — the yaml is
  now the ratified contract for the whole v0.6 surface

### Added — M6-08 the contract gate
- OpenAPI diff test: `implemented == contract` — `KNOWN_GAPS` emptied to ∅ at
  M6-03 and kept green; scoped to the non-platform/non-§4n paths (34 = the
  canonical 32 + 2 org-member paths), excluding `/platform/support/*` (v0.7),
  the §4n paths (v0.9) and `/api/compat/*` (M6-09) by design; extended and
  re-greened at the M6-10 ratification

### Verification
- Full CI gate (Postgres/Timescale + Redis): **436 passed @ 88.11% coverage**,
  exit 0; contract gate green on the ratified v1.7.0 yaml
- sqlite quick loop runs the full compat REST surface via portable
  `legacy_slug_map`/`saloon_telemetry` DDL replays (test.py's
  "create from state" intent implemented); `DISTINCT ON` given a portable
  vendor branch
- Compat: 18/18 (WS) + C1–C16 matrix; ops: 34-test suite; WS: 17/17 —
  all green on both backends
- WALKTHROUGH demo script extended to steps 1–7 (`versions/v0.6/WALKTHROUGH.md`);
  recording waived by owner

### Deployment notes
- Channels requires the ASGI entrypoint (`config/asgi.py`) behind Daphne/
  Uvicorn; Redis must be reachable for the WS cap + group layer
- `DATA_UPLOAD_MAX_MEMORY_SIZE` raised to 12 MB for O6 photo uploads (10 MB
  contract ceiling + headroom); `MEDIA_ROOT` must be a writable volume
- New settings: `REST_FRAMEWORK.DEFAULT_THROTTLE_RATES['auth_strict']`;
  throttle counters live in shared Redis — a wiped cache resets them (by
  design, documented in the O10 tests)

## [Unreleased → 1.6.0] — Flutter compat adapter (draft, Tracker §4o)

Shipped inside v0.6 (M6-09) ahead of the 1.6.0 tag: additive compat layer so
the CURRENT Flutter build works against this backend unchanged. Nothing in the
v1.5.0 REST surface changed. Promoted to `[v0.6]` above.

## [Planned → 1.7.0] — ops read APIs (draft, Tracker §4p)

Shipped inside v0.6 (M6-10) ahead of the 1.7.0 tag: pagination convention,
O1–O12, `user_sessions`, `diag:last:{slot}`, yaml ratification. Promoted to
`[v0.6]` above.

## [v0.5] — 2026-09-02 — provisioning + zero-touch trust boundary (M5)

Unapproved hardware can never inject data: gateway identity, telemetry routing
and broker credentials all key off the provisioning lifecycle now.

### Risk killed
**Trust boundary.** Telemetry from a gateway that is not yet approved is HELD
in a FIFO pending stream (never published, never dropped); a rejected or
suspended gateway's telemetry is archived to the DLQ with a machine reason;
EMQX ACLs are revoked on suspension/rejection (no_match=deny) so the broker
itself drops its traffic; credential rotation is staged and committed ONLY on
device ACK — no lockout, no plaintext re-issue.

### Added — M5 provisioning + zero-touch
- **M5-01 transition service** in `apps/provisioning/services.py`: atomic
  status + `provisioning_audit_log` row + status-cache invalidation; every
  lifecycle action goes through it (no direct `provisioning_status` saves)
- **M5-02 gateway-request flow**: manager-submitted requests with config-cap
  (`max_pending_gateway_requests`), platform review — admin/support by default
  via the new `User.staff_role` axis (accounts migration 0002), extensible
  via an optional `PlatformPermission` model (stub) — approval creates slot +
  bcrypt credentials + EMQX allow-rule; plaintext password issued exactly once
- **M5-03 installer sync**: `POST /api/v1/provisioning/sync/` — zones,
  sensor-slot mappings (hardware derived server-side, forged ids ignored),
  hardware `replacements` (retire the old unit's registry row, re-point the
  pack under the new unit, drop the stale mapping — validated before ANY
  write so a malformed list can't leave partial topology), per-slot Redis
  lock → `PENDING_APPROVAL`; second sync → 409 + Retry-After
- **M5-04 telemetry trust boundary** in the subscriber pipeline: status-aware
  routing — `PENDING_APPROVAL` → `telemetry:pending_approval` FIFO, `REJECTED`/
  `SUSPENDED` → DLQ archive (`gateway_rejected` / `gateway_suspended`), no
  record (legacy/demo) flows normally
- **M5-05 FM lifecycle**: approve (drains held FIFO into Postgres in order),
  reject (archives held telemetry to the DLQ), suspend (revokes ACLs),
  resume (re-syncs allow-rules); Blueprint §11 state matrices enforced → 409
  on illegal transitions, zero partial writes
- **M5-06 gateway config**: `GET /api/v1/gateways/{id}/config/` — R2 contract
  payload (org/farm ids, MQTT endpoint, credential hash + rotation_pending),
  no sensor slot_ids; pre/post approval works; 403 on rejected/suspended;
  dual auth per TASKS: the gateway itself authenticates with HTTP Basic
  against its bcrypt credential (zero-touch bootstrapping) OR a farm
  manager/owner fetches with the normal JWT
- **M5-07 EMQX ACL wiring** (`apps/mqtt_auth/services.py`): built-in-DB
  authorizer sync from `mqtt_acl_rules`; revocation empties rules ⇒ broker
  denies; EMQX failures logged and FNV-soft (DB rules remain the truth).
  NEW: the gateway's **authn user** is created at approval with the one-time
  plaintext (EMQX 5.5 rejects pre-hashed `password_hash` — verified live)
  and probed on rule sync; review approval pins the real publish topics
  (`{org}/farms/{farm}/gateways/{slot}/{telemetry,status}` — the old
  `org/.../gateway/#` shape matched nothing under no_match=deny)
- **M5-08 staged rotation**: stage (pending bcrypt hash, old credential
  stays live), commit-on-ACK (promotes ONLY when the device presents the new
  password), 24 h stale-rotation beat sweep; rotation commit pushes the new
  password to the EMQX authn user (404-heal self-converges a wiped broker);
  request-approval credentials now hashed with EMQX-native `$2b$` bcrypt
  (C7) instead of `bcrypt_sha256`; dev/CI EMQX profile now deny-by-default
  with a bootstrap API key, and the demo provisioner migrates the authn
  chain to bcrypt
- **Live broker verification** (`scripts/live_verify_emqx.py`): the
  M5-07/M5-08 broker semantics CI can only mock were proven against real
  EMQX 5.5.1 — plaintext user creation, wrong-password CONNECT rejection,
  allow-rule publish delivered, revoke-to-empty drops delivery,
  rotation (new password connects, old rejected); 10/10 checks green
- **Real-EMQX CI job**: `docker-compose.ci.yml` now carries an `emqx`
  service (dev-profile broker: built-in-DB authn, deny-by-default authz,
  bootstrap API key) plus a `live-verify` job that provisions it via
  `emqx_provision.py` and runs `scripts/live_verify_emqx.py` against it —
  `docker compose -f docker-compose.ci.yml run --rm live-verify`, so
  M5-07/M5-08 broker semantics stay proven against a real broker in CI,
  never only mocked
- **Demo dashboard** completed (`apps/telemetry/demo_views.py`): identity,
  sensor health, rolling telemetry stats, per-sensor-slot breakdown, EMQX
  status, simulate + farm topology + seed command (`demo_farm_seed`)
- Subscriber boots resilient to broker downtime (2 s retry), subscribes to
  the status topic, mirrors LWT into pack-liveness keys

### Verification
- CI gate (`docker compose -f docker-compose.ci.yml run --rm test`):
  **284 passed, 3 skipped, 89.24% coverage** on
  timescale/timescaledb:2.29.2-pg16 + redis:7-alpine (fresh test DB,
  no background test container racing the runner)
- Test-suite Redis URLs now derive from the env URL (host `localhost` locally,
  `redis` service name in Docker) instead of hardcoded scratch URLs; CI Redis
  URL dropped the invalid `/0` suffix
- Fresh CI volumes get `morghdari_app` table grants applied at session setup
  (explicit GRANT on ALL tables — default privileges only cover future
  tables, so a brand-new test DB was invisible to the app role)

### Deployment notes
- MQTT auth services speak to EMQX 5.x management API
  (`EMQX_API_URL`/`EMQX_API_USER`/`EMQX_API_PASSWORD`, default
  http://127.0.0.1:18083 demo-admin/dev_password); ACL enforcement is
  FNV-soft — a broker outage logs and leaves DB rules as source of truth
- Provision the broker for the demo with `scripts/emqx_provision.py`
  (authn users + per-farm ACL rules; shape pinned by `test_emqx_provision.py`);
  `no_match=deny` is set at broker bootstrap
- New env contract for the subscriber: `SUBSCRIBER_CLIENT_ID` (required),
  `MQTT_HOST`/`MQTT_PORT`/`MQTT_USERNAME`/`MQTT_PASSWORD`, `REDIS_URL`
- Gateway config payload carries `rate_limit_note`: throttle MQTT CONNECT +
  publish per gateway at the reverse proxy (nginx `limit_req`); broker-level
  quotas land with the v1.0 quota engine

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
