# CHANGELOG

All notable changes per version. Format follows the spiral-roadmap discipline:
one section per version tag, each listing what risk the version killed.

## [v0.8-DR] — 2026-09-12 — disaster recovery, backend half (M8-DR)

Recovery is rehearsed, not documented: point-in-time restore and Redis-wipe
recovery both proven live on the rehearsal stack with measured RTOs. Contract
untouched (no surface added in v0.8-DR).

### Risk killed
**Unproven recovery.** Before v0.8-DR, PITR was a runbook nobody had run
(no wal-g in the image, no archive, no base backup) and the Redis-wipe path
was code nobody had executed (the rebuild service was still a parked stub in
the running images). Now the base backup exists, WAL flows continuously, the
T2 restore reaches a chosen instant with row parity, and the T3 wipe re-arms
alerting in minutes — each step measured against its RTO.

### Added — M8-01 wal-g bake-in + M8-02 T2 live PASS
- wal-g v3.0.9 baked into the postgres image (Alpine rebuild: apk + gcompat +
  real asset filename), `archive_mode=on`, MinIO rehearsal target, weekly
  base-backup script; base pushed, WAL verified flowing.
- T2 live: target 13:37:57+00, marker absent, parity 11/2/2, hypertables
  present, RTO ~10 min (plan ≤ 240). DR-LOG row 1.

### Added — M8-03 T3 live PASS + M8-04-H sim-green
- Real volume wipe → rebuild clean on the new code → 6/6 threshold hashes
  with full bands → HIL P2+P4 end-to-end after the wipe, RTO ~5 min
  (plan ≤ 15). Run 1 VOID (fake wipe — documented, not hidden). DR-LOG rows 2–3.
- Readiness 8/8 + harness sim 4 PASS + 2 honest SKIP (P5/P6 need hardware).

### Fixed along the way (drill-found, all in-tree)
- Custom `config_file` repeats `listen_addresses` + timescaledb preload;
  PG libpq env for wal-g; `backup-fetch LATEST`; `-p morghdari` typo;
  T2 backup guard + correct fetch ordering; T3 `rm -f` + fatal wipe +
  synchronous legs; flock lookups join the admin-alias routing (M8-03b);
  six HIL-harness sim bugs + `--hardware-id` / `--mqtt-observer-user/pass`.

### Verification
- Transcript `versions/v0.8/DEMO-M8-DR.md`; DR log `versions/v0.8/DR-LOG.md`.
- Post-drills: all services healthy, 0 pending migrations, subscribers
  SUBACK-granted. Release note `docs/releases/v0.8-DR.md`.

### Scope note
- M8-04/05/06 parked as `v0.8-HIL` (hardware-team owned, backend-only close
  per owner 2026-09-12). Tag `v0.8-DR` (push waived — no remote).

## [v0.7] — 2026-09-10 — production birth on the laptop (M7)

The system runs on real hardware and screams when it is sick: laptop-as-VPS rehearsed, metrics/bridges/heartbeats live, native lists + reports merged, support activated, compat scoped down. Contract gate: `implemented == contract`.

### Risk killed
**Silent sickness on unproven prod.** Before v0.7 the stack never ran as production: no `/api/v1/metrics/` (the runbook promised it, the code never had it), no burn-rate rules, no native farm/saloon lists, no reports surface, dormant support ops, unscoped compat. Now the 9-container prod compose runs on the laptop, Prometheus watches 10/10 targets, DLQ spikes page, rollbacks are drilled, and every DoD leg is proven live over real HTTPS/TLS.

### Added — M7-01..M7-06 laptop-as-VPS rehearsals (2026-09-08)
- Port discipline 80/443/8883, staged TLS certs under both naming conventions, 9-container prod compose healthy, monitoring loopback with 8 Prometheus targets UP, backup+restore drill (3 script bugs fixed by the drill), shared-sub rebalance (4 systemic prod bugs fixed)
- Rehearsal fixes committed in `a9dd05e` (M7-03 stack), `da9dd05e` (M7-05 scripts), `f48e8dd` (M7-06 pipeline)

### Added — M7-04 observability seam
- Django `/api/v1/metrics/` implemented (runbook promised it, code never had it — `1b3c19f`) + Redis bridge + beat heartbeat; `DlqSpike` rule added

### Added — M7-07 support activation (MFA + 3 ops)
- Staff TOTP MFA enforced (`pyotp`, two-step setup→confirm) + 3 support ops (gateway suspend/resume with ACL revocation, alert force-resolve idempotent, task drain whitelisted) + contract-gate `/platform/` exclusion shrunk
- Full CI 452 passed @ 88.59%

### Added — M7-08 MET-AVAIL + MET-SAAS
- MET-AVAIL: 2 blackbox vantages, burn-rate rules vs `probe_success`
- MET-SAAS: hourly meter key, nightly `build_org_usage` → `org_daily_usage`, `tenant_cost_estimate_usd` export
- 8-test suite; full CI 452 @ 88.59%

### Added — M7-09 chaos drill
- DLQ spike 104 → FIRING → cleared; `scripts/deploy.sh <tag>` rollback drilled for real (~35 s, auto-rollback on failed gate)

### Added — M7-11/M7-12 native v1.6 lists
- **M7-11** `GET /api/v1/farms/` + `/api/v1/saloons/` (`?org_id=`/`?farm_id=`, foreign → 403)
- **M7-12** `GET /farms/{id}/telemetry/` aggregate + `?limit=`/`?offset=` newest-first (`?limit=1` IS latest)
- Combined gate 434 @ 89.83%

### Added — M7-13 frontend migration proof (2026-09-09)
- Native-ID session, 3 hits 200, ZERO compat/legacy hits in the nginx log

### Added — M7-14 compat removal SCOPED per owner
- C1–C15 REST handlers deleted (now Django 404), W1–W5 + C16 WS + `legacy_slug_map` retained; `urls_compat` still mounted

### Added — M7-15/M7-16 reports-merge (Tracker §4s)
- **M7-15** reports base: 4 tables (`daily_reports`, `report_medication_usages`, `vet_medication_catalog`, `support_tickets`) + v1.5 endpoints + FLAGGED auto-ticket + WS push + D24-3 Tehran time-lock; full CI 488 + 3 skipped @ 88.74%
- **M7-16** `daily_report_extras` 1:1 side table, alias normalization, checklist 400, GET by-date, `is_completed`/`reported_by` aliases

### Added — M7-17 aliases + v1.7.0 ratification
- W1–W5 compat aliases + v1.7.0 ratification (yaml absorbs compat routes); contract union stays v1.7.0 (owner §4s decision; 1.8.0 reserved for growth)

### Added — M7-10 DoD live-drill fixes (2026-09-10, proven live on the rehearsal stack BEFORE closing)
- (1) `reports.0001` created `vet_medication_catalog` AFTER its FK dependent → real migrate died `UndefinedColumn` (never caught: test matrix runs `--nomigrations`, migrate-gate last ran pre-M7-15)
- (2) subscriber logged subscribed when the SUBSCRIBE was only queued — a boot race left both subscribers 74 min with zero broker-side subs, PUBs ACK'd into the void; now SUBACK-verified + 5 s resubscribe retry + `subscriber_subscribed` gauge
- (3) support-session opener did a bare org lookup — staff tokens are org-less (D15) and `organizations` is FORCE-RLS'd → every open 400'd on prod; now atomic + `tenant_context`
- (4) reports models lacked `managed=False` (docstring claimed unmanaged) + no migration state → `makemigrations --check` red; state folded into `0001`/`0002`

### Verification
- Full CI gate (Postgres/Timescale + Redis): **513 passed, 3 skipped @ 88.09%**, exit 0; module coverage gate GREEN; contract gate green (`implemented == contract` on the ratified v1.7.0 yaml, `KNOWN_GAPS` empty)
- Migrate gate RESULT: DEPLOYABLE (69 migrations forward + idempotent re-run + every app unwinds to zero)
- Live drills 10/10 legs green over real HTTPS/TLS: lists, paging newest-first, farm==saloon-union, TLS publish end-to-end + trust-boundary DLQ `TOPIC_PAYLOAD_IDENTITY_MISMATCH`, reports DRAFT→SUBMITTED→APPROVED + FLAGGED auto-ticket + checklist 400 + W-alias byte-compat + C-route 404s, MFA enroll→confirm→enforce + wrong-code 403, suspend→PUB-denied→resume→flows, force-resolve idempotent, drain 202, 10/10 Prometheus targets UP, rollup counts exact

### Deployment notes
- Rebuild images after the M7-15 wave (stale-image 404 lesson)
- nginx reload after recreating app containers (upstream IP cache — third confirmation)
- Rehearsal-DB migrate as the ADMIN role (as-app fail-closed on schema `public`)
- EMQX authn needs a `gateway_slots` row with valid `provisioning_status` (CONNACK gate)
- Subscriber needs `MQTT_TLS_INSECURE=1` staging-only
- Tenant login over app role still P0 (RLS pre-GUC, M7-13 finding, VPS-day)
- Drill GW credential re-seeded during testing

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

## [1.7.2] — 2026-09-12 — follow-through core: topology writes, the cycle writers, mutes (F1–F15)

An audit of the shipped surface found four things the ENGINE needed that no API
could write: `flock_cycles.end_date` (closing a cycle was impossible, while the
KPI windows, the quality verdicts and the threshold hash all key off it),
`mortality_records` (KPI mortality could only ever read zero while daily reports
stored their own count), `maintenance_mutes` (read by every alert evaluation,
writable only from the ORM — the model said "via Django Admin" and no admin
registration ever shipped), and the org's own topology (nothing could create a
farm/saloon/position except SQL). This wave implements the documented
follow-through CORE (`docs/api-followthrough-v1.md` F1–F15) plus the member role
change. Additive-only: **zero new tables, zero migrations**, no change to any
existing path.

### Added — tenant topology writes
- `POST /farms/` (F1, owner) and `PATCH|DELETE /farms/{id}/` (F2/F3, owner):
  geography validated (village must belong to the city), delete refused with
  `409 farm not empty`.
- `POST /farms/{id}/saloons/` (F4, manager+) and `PATCH|DELETE /saloons/{id}/`
  (F5/F6, manager+): a saloon is deletable only with no flock history at all and
  no telemetry history (the schema is stricter than the spec's "no active flock"
  — see the fix below).
- `POST /saloons/{id}/positions/` (F7) and `PATCH .../positions/{pid}/` (F8,
  manager+), duplicate label `409`.
- **Every farm/saloon creation mints its `legacy_slug_map` row in the same
  transaction**, so the Flutter compat aliases resolve a brand-new farm at once;
  the slug convention now lives in one module (`apps/compat/legacy_slugs.py`)
  that the ops backfill command calls too.

### Added — the two writers the engine was missing
- `PATCH /saloons/{id}/flocks/{fid}/close/` (F14, engineer+): sets `end_date`,
  the only writer there is, and the save re-arms the saloon's threshold hash
  through the existing post_save fast path. Only the ACTIVE cycle closes.
- `POST /saloons/{id}/flocks/{fid}/mortality/` (F15, engineer+): writes
  `mortality_records` — the table `/saloons/{id}/kpis/` computes `mortality_pct`
  and its daily series from. Direct entry sits BESIDE the daily-report counts.

### Added — reads and windows
- `GET /alerts/` (F9, engineer+): the org-wide feed (farm/saloon/status/tier/
  metric filters, `{results, count}`) beside the per-saloon read.
- `GET /overview/` (F10, any member): multi-farm home rollup with totals.
- `GET|POST /maintenance-mutes/` + `DELETE /maintenance-mutes/{id}/` (F11–F13,
  manager+): first REST surface for the mute table; ≤72 h, no overlap per
  saloon, revoke early.

### Added — members + contract
- `PATCH /org/members/{member_id}/` (owner-only): role change with a self-change
  refusal (an owner demoting themselves would lock the org out) and live refresh
  lines revoked in the same transaction (claims are frozen at token birth).
- `reviewer` is now GRANTABLE: the v1.5 reports wave defined the role but the
  invite enum rejected it, so no org could produce one.
- **Erratum ratified**: the four manager-side gateway lifecycle twins
  (suspend/resume/rotate-credential/rotation-ack) shipped in v0.5 and were never
  documented; they are in the yaml now and the contract gate's `ERRATUM_PENDING`
  set is EMPTY.
- Contract yaml ratified 1.7.1 → **1.7.2** (+20 paths, 1 new component); the
  generated mirror was regenerated in the same wave.

### Fixed along the way
- The module-coverage gate only recognised `api.py`, so the new
  `api_follow.py`-style modules would have been invisible to it — the exact
  aggregate-hole class the gate was written for (widened to `api*.py`).
- A refused delete no longer leaves a `*_DELETED` audit trail: the audit row is
  written only after the delete succeeds (the FKs are RESTRICT, so deletions
  carry their ids in `details`).
- Hard deletes are judged by a shared guard (`services_follow.delete_row`) that
  refuses on a backend whose history tables do not exist instead of dying inside
  Django's collector.
- **F6 promised a delete the database refuses.** `flock_cycles.saloon` is
  `ON DELETE RESTRICT`, so a saloon that ever housed a flock — a CLOSED cycle
  included — can never be hard-deleted. The sqlite test loop could not see it
  (Django's collector only emulates that constraint), so the first Postgres run
  of `test_followthrough_core.py` found the guard reaching a row the DB would
  never release. The guard now reports the real rule in three distinct verdicts
  (active flock / flock history / telemetry history), and the Postgres-only test
  `test_saloon_delete_refuses_when_measured` pins the telemetry branch the sqlite
  loop cannot exercise.

## [1.7.1] — 2026-09-12 — threshold-template write surface (O13–O16)

Owner decision 2026-09-12: the seeded-only template rule is lifted, so a manager
can tune bands per breed without a deploy and an engineer can prepare next
season's plan while the current flock is still armed.

- `POST /threshold-templates/` (O13): an engineer authors DRAFTS strictly inside
the farms granted via `account_role_scopes` — fail-closed, no scope row means no
write. Bands are validated (known metric, day window in [0, 400], O3 sanity
bounds, `nh3` never unbounded, no overlapping windows per metric).
- `PUT /threshold-templates/{id}/` (O14): edit a draft; `rules` replaces the band
list as a set; a PUBLISHED template is `409` (its bands are tuned through O3).
- `POST /threshold-templates/{id}/publish/` (O15): owner/manager only, fires the
alerts cache sweep; `409` when the breed or org-default slot is taken — the
guards are DB constraints, not conventions.
- `GET`/`PUT /saloons/{id}/threshold-template/` (O16): explicit assignment
(`null` clears) beside the effective resolution, re-caching that saloon.
- **A draft never arms a saloon**: resolution considers PUBLISHED rows only, so
the write surface cannot move a live band behind a manager's back.
- Contract yaml ratified 1.7.0 → **1.7.1** (+4 paths, 3 new components); the
mirror was regenerated in the same wave. See `docs/api-ops-v1.md` (v1.7.1
addendum).

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

## [Planned → 0.8.0] — DR + real-hardware HIL (M8, v0.8 start)

Started 2026-09-12 on `v0.8-m8` (owner go — hardware team procurement runs in
parallel; the ESP32 itself is the hardware team's deliverable).

### Added — M8-01 wal-g bake-in (DR §2.1/§3.3)
- `docker/postgres-walg/` image: timescale + wal-g v3.0.9, `archive_mode=on`,
  `archive_command=wal-g wal-push %p`, `archive_timeout=300` (RPO ≤ 1 h math)
- Prod compose postgres → the wal-g image + WALG/AWS env block; rehearsal
  override builds locally + adds MinIO (S3-compatible wal-g target) so the T2
  drill runs without cloud S3 creds
- `scripts/walg_base_backup.sh` — weekly base backup + `backup-list` evidence

### Added — M8-02 T2 PITR drill (DR §3.1)
- `scripts/pitr_rehearsal.sh` — the full drill executable end-to-end: parity
  baseline → WAL switch → target time → stop writers (incl. EMQX) → marker
  row → vol rm → `wal-g backup-fetch` → restore_command + recovery_target_time
  + `recovery.signal` → replay-pause wait → marker-absent + row-parity +
  hypertable verify → promote → restart → RTO measured + DR-log row
- Post-drill verification covers the v1.6.0 tables (`legacy_slug_map`,
  `daily_report_extras`) per Tracker §4o/§4s

### Added — M8-03 T3 Redis-wipe drill (DR §3.2)
- `alerts.rebuild_active_state_from_db` IMPLEMENTED (Tracker §4l Q10 park
  lifted): re-creates breach + active-alert keys from Postgres truth; LOS and
  REPLAY_ANALYSIS rows deliberately untouched; admin-or-default alias routing
  (5-test module `test_m8_03_rebuild_drill.py`)
- `thresholds.recache_*` routed through the admin alias — FIXES the M4-latent
  no-op where the FORCE-RLS worker's midnight threshold backstop saw zero
  saloons cross-org
- `scripts/redis_wipe_drill.sh` — volume rm → rebuild leg → threshold warmer →
  LOS suppression window → breach evidence → RTO measured + DR-log row

### Added — C5 backend HIL readiness + Hardware Handover Harness
- `scripts/check_hil_readiness.py` (health/hardware-time/config-route probes +
  O9/T5–T9/OTA/LOS static gates) and `scripts/ota_checksums.py` (T6 manifest)
- `scripts/hil_handover/hil_harness.py` (+ README) — receiver-side executable
  acceptance for the hardware team: P1 config fetch, P2 telemetry honesty,
  P3 LWT, P4 buffered replay w/ ORIGINAL timestamps, P5 OTA, P6 rotation;
  `--target sim` (self-test, P5/P6 SKIP) vs `--target real` (the handover gate)
- `versions/v0.8/DR-LOG.md` — repo-local mirror of the DR Plan §6 log

### Status
- M8-01/02/03 + C5 + harness IMPLEMENTED on `v0.8-m8`.
- **Scope cut 2026-09-12 (backend-only close as `v0.8-DR`, owner go):**
  M8-04/M8-05/M8-06 PARKED — no ESP32/firmware hands-on for the backend owner;
  hardware-team owned as `v0.8-HIL` follow-up. Close claims the DR half only
  (2 DR-log rows + `--target sim` proof), NOT the real-radio exit demo.
- **Live 2026-09-12 (rehearsal): T2 PASS** (target 13:37:57+00, marker absent,
  parity 11/2/2, RTO ~10 min) **+ T3 PASS** (real volume wipe, rebuild clean,
  6/6 threshold hashes, HIL P2+P4 end-to-end after the wipe, RTO ~5 min).
  Drill-found fixes: wal-g image Alpine rebuild, MinIO→quay.io, PG libpq env,
  backup-fetch LATEST, config_file must repeat listen/preload, flock lookups
  join the admin-alias routing (M8-03b), drill legs synchronous. Rows:
  `versions/v0.8/DR-LOG.md`.
