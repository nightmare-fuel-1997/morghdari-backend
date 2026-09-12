# Topology Write APIs — Endpoint Spec (Contract v1.10.0-draft, App v0.8 scope extension)

> **Status:** DRAFT 2026-09-12 · **Additive-only** — no `ALTER` on frozen tables, no change to
> existing `/api/v1` paths, no change to the gateway state machine. One small new table
> (`topology_refs`, §7); everything else writes tables that already exist.
> **Canonical homes:** this file (v1.10.0 surface) · plan: `VERSIONS.md` §v0.8 (scope addition) ·
> release draft: `docs/releases/v1.10-topology-draft.md` · history: `CHANGELOG.md [Planned → 1.10.0]`.
> **Sibling specs:** `docs/api-followthrough-v1.md` (v1.9.0 tenant CRUD F1–F8) ·
> `docs/api-ops-v1.md` (v1.7.0 staff/ops reads O1–O16).
> **Consumer that forced this:** the offline simulator (`M:\projects\poultry\simulator`), which must
> create orgs/farms/saloons/sensor slots, install gateway hardware, and bind/unbind sensors through
> real interfaces — never through raw SQL.

---

## 0. The decision: does the backend expose this as a real ops surface?

**Yes — as a staff-only platform surface, not tenant CRUD.** Three consumers already need it, and
two of them are not the simulator:

1. **Hardware team / installer path.** Today nothing can create a `gateway_slots` row with
   `PENDING_INSTALLATION`, and `POST /api/v1/provisioning/sync/` refuses with
   `400 active gateway hardware not registered` because no endpoint in the codebase writes
   `gateway_hardware_registry`. The HIL milestone (v0.8) needs that path to mount a real gateway.
2. **The simulator** (fair-weather and storm runs, multi-org, sensor churn).
3. **Ops backfill / DR re-seed.** `scripts/start_dev_stack.sh:14` states the current truth:
   *"No seeder ships in this repo any more: the farm/saloon topology is created by the operator
   (Django admin or API)."* Today "Django admin" is the only real answer; that is unauditable,
   untestable under RLS and unrepeatable.

**Options weighed**

| Option | What it is | Verdict |
|---|---|---|
| **A. Staff-only ops provisioning surface** (`/api/v1/ops/provisioning/…`, §2) | Real endpoints, `topology_write` codename, audited, idempotent, bulk-capable, runs as the app role under `tenant_context` | **RECOMMENDED** — closes a product gap the hardware team already has; identical code path for staff, simulator and backfill; every write lands an audit row and is exercised under FORCE RLS |
| B. Simulator-only ORM shim (`manage.py shell` / direct DSN) | Sim writes rows itself with the admin connection | **Rejected as the primary path** — runs BYPASSRLS (so isolation and RLS stay untested), writes no audit rows, has no validation, and breaks silently on the next schema change. Keep only as §9's explicit fallback |
| C. Re-introduce an in-repo seeder command | `manage.py seed_topology --file manifest.json` | **Rejected** — dev-only by construction (not reachable on staging/prod where the simulator must run), and it hides the write surface from the contract gate |
| D. Extend `provisioning/sync/` to invent topology | Let the installer role create slots/hardware | **Rejected** — violates the rule that doc already states: the body *"names a slot in the caller's org, never topology of its own invention"*. Installer tokens are tenant-scoped and cannot create orgs or farms |
| E. Do nothing; simulator runs against a hand-seeded stack | Owner seeds via Django admin per run | **Rejected** — every bench tier needs hundreds of orgs/farms/slots; hand-seeding makes the simulator unrepeatable, and reruns drift (the exact failure the client-ref rule in §3 exists to prevent) |

**Scope boundary:** tenant-facing farm/saloon creation stays where it is planned — **F1–F8 in
`docs/api-followthrough-v1.md` (contract v1.9.0, "owner creates farms", Tracker §4d R3-ACTOR-MAP)**.
This spec adds the staff/bulk/hardware half and must share one service layer with F1–F8 so delete
guards, slug handling and `org_id` denormalization can never diverge.

---

## 1. What is missing today (evidence)

| Missing write | Consequence today | Source of truth |
|---|---|---|
| Org creation + first owner membership | Ops onboards orgs through the Django admin only | `apps/tenants`, no API |
| Farm / saloon / position creation | v1.9 F1–F8 drafted, not implemented; ops-assisted onboarding is docs-only | `docs/api-followthrough-v1.md` §5 note; `docs/api-ops-v1.md` §5 |
| `saloon_sensor_slots` creation / deactivation | Installers cannot register a sensor slot; `is_active` has no writer | `apps/devices/models.py` (`SaloonSensorSlot`), read-only surface |
| `gateway_slots` creation outside the request queue | Only `review_gateway_request(APPROVE)` mints a slot; no import/bulk/backfill path | `apps/provisioning/api.py` (~L470) |
| `gateway_hardware_registry` install/retire | `provisioning/sync/` 400s with `active gateway hardware not registered` — the dead end **before** the first sync | `apps/provisioning/api.py` (`provisioning_sync`) |
| `hardware_registry` (sensor) install/retire | Swap history for sensor packs is admin-only | `apps/devices/models.py` (`HardwareRegistry`) |
| `gateway_sensor_mapping` **unbind** | `provisioning/sync/` upserts mappings and deletes stale ones only inside a `replacements` swap; there is no "this pack left this slot" call | `apps/provisioning/api.py` |
| Topology export | The simulator's `--manifest` (and `scripts/simulate_farm.py`) is hand-written and drifts from the DB | `scripts/start_dev_stack.sh:14` |

---

## 2. Route table (all `BearerAuth`, all staff-only — §3.1)

Prefix `ops/provisioning/` keeps the staff boundary legible and cannot collide with the tenant
routes v1.9 adds under `farms/` and `saloons/`.

| # | Method + Route | Request | Response |
|---|---|---|---|
| T1 | `POST /api/v1/ops/provisioning/orgs/` | `{name, slug?, client_ref, owner:{username, email?, password?}}` | `201`: `{org_id, owner_user_id, username, password?}` (`password` returned once when generated; never stored in plaintext). `200` + identical ids when `client_ref` is already known |
| T2 | `POST /api/v1/ops/provisioning/farms/` | `{org_id, name, province_id, city_id, village_id?, client_ref}` | `201`: `{farm_id, org_id}`. `400` unknown geography (geography stays global reference data, read via the existing `GET /provisioning/provinces\|cities\|villages`) |
| T3 | `PATCH /api/v1/ops/provisioning/farms/{farm_id}/` | `{name?, is_active?}` | `200`: updated + audit row. **No delete route** (§3.4) |
| T4 | `POST /api/v1/ops/provisioning/saloons/` | `{org_id, farm_id, name, client_ref}` | `201`: `{saloon_id}`. `404` unknown/foreign farm (never an existence oracle) |
| T5 | `POST /api/v1/ops/provisioning/positions/` | `{org_id, saloon_id, label, client_ref}` | `201`: `{position_id}`. `409` on duplicate `UNIQUE(saloon_id,label)` |
| T6 | `POST /api/v1/ops/provisioning/sensor-slots/` | `{org_id, saloon_id, position_id, slot_id, allowed_metric_keys[], client_ref}` | `201`: `{slot_id, is_active:true}`. `409` slot id taken; `400` empty/unknown metric key |
| T7 | `PATCH /api/v1/ops/provisioning/sensor-slots/{slot_id}/` | `{is_active?, allowed_metric_keys?}` | `200`: updated. `is_active:false` = **logical sensor removal** (history, telemetry and alert rows untouched) |
| T8 | `POST /api/v1/ops/provisioning/gateways/` | `{org_id, farm_id, slot_id, position_label?, request_id?, client_ref}` | `201`: `{gateway_slot_id, provisioning_status:"PENDING_INSTALLATION"}`. `request_id` links an existing request (lifecycle audit stays intact); **status changes still go through the existing approve/reject/suspend/resume/rotate paths** |
| T9 | `POST /api/v1/ops/provisioning/gateways/{slot}/hardware/` | `{hardware_id, client_ref}` | `201`: `{hardware_id, installed_at}` — writes the `gateway_hardware_registry` row that unblocks `provisioning/sync/`. `409` when that hardware is already active on another slot |
| T10 | `DELETE /api/v1/ops/provisioning/gateways/{slot}/hardware/{hardware_id}/` | — | `200`: `{removed_at}` (retire, never delete; history preserved for swaps) |
| T11 | `PUT /api/v1/ops/provisioning/gateways/{slot}/sensor-mappings/` | `{gateway_hardware_id, mappings:[{pack_id, sensor_slot_id}]}` (full replacement of that hardware's pack set) | `200`: `{bound:[pack_id], unbound:[pack_id]}`. `400` unknown/foreign hardware or sensor slot; `409` a pack already bound to different hardware in the same request set |
| T12 | `DELETE /api/v1/ops/provisioning/sensor-mappings/{gateway_hardware_id}/{pack_id}/` | — | `200`: unbound (mapping row deleted + audit row). **This is the missing "remove sensor from gateway"** |
| T13 | `POST /api/v1/ops/provisioning/topology/` | `{org_id, dry_run?:false, nodes:[{kind, client_ref, …per-kind fields}]}` (≤200 nodes/request, §3.3) | `200`: `{created, updated, skipped, ids:{client_ref → entity id}, plan?}` — the simulator's primary path |
| T14 | `GET /api/v1/ops/provisioning/topology/` | `?org_id=&format=manifest` | `200`: manifest JSON (org, farms, saloons, positions, sensor slots, gateway slots, hardware, mappings) — the same shape `scripts/simulate_farm.py --manifest` already consumes |
| T15 | `POST /api/v1/ops/provisioning/sensor-hardware/` · `DELETE …/sensor-hardware/{hardware_id}/` | `{org_id, sensor_slot_id, hardware_id, client_ref}` | `201`/`200`: installed / retired (`hardware_registry.removed_at`) — real install history for HIL runs |

---

## 3. Write semantics (the rules that make this safe to hand a load generator)

### 3.1 Role gate — staff only, never a tenant token

- New platform permission codename **`topology_write`** resolved exactly like the existing
  `gateway_review` extension point (`apps/provisioning/api.py::_can_review`): superuser, or
  `user.is_platform_staff and staff_role ∈ {admin, support}`, or a `PlatformPermission` row.
- A tenant token — *including an owner token* — gets `403 {"error":"topology_write required"}` on
  every T route. Tenant onboarding remains F1–F8's job.
- Staff lane rate: `10 r/s`; the bulk route (T13) is the intended path for volume, so per-row
  traffic stays negligible.
- Simulator credential handoff: one platform-staff account per environment, password supplied by
  env var (`SIM_OPS_USER`/`SIM_OPS_PASSWORD`, the same never-hardcoded rule as the MQTT password).

### 3.2 Idempotency — `client_ref` on every create (the property that makes benchmarks repeatable)

Every create accepts a caller-owned stable key (`client_ref`, e.g. `sim1:r7:org3:farm2:saloon7`).
A repeat call with a known `(org_id, kind, client_ref)` returns the **existing** row
(`200`, same ids) instead of creating a twin. Backed by `topology_refs` (§7).

Without this, re-running a bench tier silently doubles a topology, and a "1 org" run against a
stack that already ran becomes a "2 org" run — exactly the measurement error the benchmark exists
to eliminate.

### 3.3 Atomicity, chunking, dry-run

- Each request is `transaction.atomic()`; a T13 chunk is all-or-nothing and ≥200 nodes is
  `413 {"error":"chunk too large"}` with `max_nodes: 200`.
- `dry_run: true` validates every node and returns the same body with `plan` populated and
  `created/updated` zeroed, writing **no** rows and **no** audit rows. The simulator's `doctor`
  preflight uses it.
- Bulk-concurrency: nodes are applied in dependency order (`org → farm → saloon → position →
  sensor_slot → gateway_slot → hardware → mapping`) with `SELECT FOR UPDATE` on natural keys, so
  two concurrent tier setups cannot interleave into duplicates.

### 3.4 Deletion philosophy — RESTRICT everywhere, no cascade, no hard delete

Farms/saloons/sensor slots/gateways are never hard-deleted here (F3's `409 farm not empty` rule is
the tenant-side twin). Removal is expressed as `is_active=false`, `removed_at`, or a mapping-row
delete — telemetry, alert instances, command logs and install history all survive. No bulk delete
route exists in this spec.

### 3.5 RLS + tenant context (the lesson from the login-under-RLS fix)

Every write opens `transaction.atomic()` and sets **`tenant_context(org_id)`** for that org before
touching org-scoped tables (`apps/tenants/context`) — a T13 request that spans orgs runs one chunk
per org with its own context. Writes execute as the **app role**, so every new endpoint is a real
test of FORCE RLS rather than a bypass. Foreign-org ids answer `404` identically to unknown ones
(no existence oracle, same rule as O-routes and F-routes).

### 3.6 Audit + credential handoff

- Every mutating call writes one `account_activity_logs` row: `TOPOLOGY_ORG_CREATED`,
  `_FARM_CREATED`/`_UPDATED`, `_SALOON_CREATED`, `_POSITION_CREATED`, `_SENSOR_SLOT_CREATED`/
  `_DEACTIVATED`, `_GATEWAY_SLOT_CREATED`, `_GATEWAY_HARDWARE_INSTALLED`/`_REMOVED`,
  `_SENSOR_MAPPING_BOUND`/`_UNBOUND`, `_SENSOR_HARDWARE_INSTALLED`/`_REMOVED`, `_BULK_APPLIED`
  (with counts), `_MANIFEST_EXPORTED`. `dry_run` writes none.
- **No credentials in bulk.** MQTT credentials stay on the existing paths
  (`review_gateway_request(APPROVE)` mints slot + credential + ACL, `rotate-credential` /
  `rotation-ack` rotate them) so the simulator exercises the real lifecycle instead of a shortcut.
  T1's one-time owner password is the single exception, mirroring the existing one-time-issue rule
  (the plaintext exists only in that response).

---

## 4. What the simulator consumes

| Situation | Path used | Mode |
|---|---|---|
| Gateway install on a farm | T8 (slot) → T9 (hardware) → `POST /gateway-requests/` + review → `provisioning/sync/` → `approve` → `rotate-credential` → `rotation-ack` → real MQTT telemetry | `scenario` |
| Multiple gateways on one farm | T13 bulk (N slots + hardware per farm), then fan-in MQTT | both |
| Multiple orgs | T1 per org (T13 ignores cross-org chunking), then per-org tokens for isolation probes | both |
| Devices disconnecting + buffer replay | No API — MQTT only + read-only reconciliation | both |
| Adding a sensor | T6 (slot) + T9-style sensor hardware + T11 bind, then publish for the new pack | `scenario` |
| Removing a sensor | T12 unbind (+T7 deactivate) then silence the pack; assert `UNKNOWN_SLOT` DLQ for any straggler publish | `scenario` |
| Bench tiers (small/medium/stress) | T13 chunks only — the only way to build hundreds of orgs/farms before a run | `bench` |
| Manifest replay | T14 export → simulator `--manifest` | both |

`doctor` preflight order: staff login → `T14` export (proves read) → `T13 dry_run:true` (proves
write permission, validation and geography) → broker reachability.

---

## 5. Error contract (same envelope as v1.5.0/v1.7.0)

`400` bad/unknown reference, empty metric key, unknown geography, malformed node ·
`401` missing/expired token · `403 {"error":"topology_write required"}` tenant token or missing
permission · `404` unknown **or foreign** id (identical, never an oracle) ·
`409` duplicate label, slot id taken, hardware already active elsewhere, `client_ref` conflict ·
`413` chunk > 200 nodes · `429` past `10 r/s`.

---

## 6. Explicit non-scope

- No tenant-facing farm/saloon CRUD (that is F1–F8, contract v1.9.0).
- No gateway state-machine bypass: no status writes other than the create-time
  `PENDING_INSTALLATION`; approve/reject/suspend/resume/rotate stay where they are.
- No hard deletes, no cascades, no bulk delete, no org merge/move.
- No credential issuance in bulk or in export (`T14` never emits passwords or ACL rows).
- No actuator slots or actuator hardware (dispatch is unchanged; Phase 2).
- No telemetry/reading writes: this surface creates topology, the broker creates data.
- No calibration tables (GS-CALIB stays owner-deferred).

---

## 7. New storage (one table, additive, reversible)

| Table | Key columns |
|---|---|
| `topology_refs` | `id BIGSERIAL PK`, `org_id UUID NOT NULL`, `kind TEXT CHECK (org/farm/saloon/position/sensor_slot/gateway_slot/gateway_hardware/sensor_hardware/mapping)`, `client_ref TEXT NOT NULL`, `entity_key TEXT NOT NULL` (the target PK, stringified), `created_by INT`, `created_at TIMESTAMPTZ DEFAULT now()`, `UNIQUE(org_id, kind, client_ref)` |

RLS: same `org_id` policy as every tenant table (add to `initdb/02_rls.sql` twin + migration), so
the refs are invisible cross-org. Migration is one `RunSQL`/`CreateModel`, reversible (drop table),
zero `ALTER` on frozen tables. `scripts/check_rls_parity.py` must stay green with the new table.

**Modules (all new files):** `views_topology.py` / `serializers_topology.py` / `urls_topology.py` /
`services_topology.py` (dependency-ordered bulk applier, client-ref resolver, delete guards shared
with F1–F8, manifest exporter).

---

## 8. Acceptance checklist (docs-side, to be ticked at implementation)

- [ ] Tenant owner token → `403` on all 15 routes; staff token → allowed; `dry_run` writes zero rows
      and zero audit rows (asserted by row counts).
- [ ] Every route green **under enforced RLS as the app role** (`SET ROLE morghdari_app`), including
      a T13 chunk that must set `tenant_context` per org.
- [ ] Re-running an identical T13 document twice: second run reports `updated/skipped`, table counts
      unchanged (client-ref idempotency proven on counts, not on responses).
- [ ] `T9 → provisioning/sync/` no longer 400s `active gateway hardware not registered` — the dead
      end is closed by test.
- [ ] `T12` unbind + a straggler publish for the removed pack lands in the DLQ as `UNKNOWN_SLOT`
      (never a silent accept, never a 500).
- [ ] `T14` manifest round-trips: export → simulator dry-run → publish accepted for the same slots.
- [ ] Foreign-org ids answer `404` on every route (no existence oracle); `409`s are the documented
      ones and nothing else.
- [ ] Contract yaml ratification: the 15 paths enter `backend/openapi.yaml`, `implemented ==
      contract` gate re-greens (`backend/tests/test_openapi_contract.py`), generated mirror passes
      `test_openapi_schema.py`.
- [ ] `docs/api-followthrough-v1.md` F1–F8 and this spec call the **same** `services_topology.py`
      guards (no duplicated delete/validation logic).

---

## 9. If Option A is not taken (simulator is not blocked)

The fallback the simulator ships with until T1–T15 exist: a `provision` step that execs
`manage.py shell` inside the stack's backend container with the admin DSN, writing the same rows in
dependency order. It works, and its costs are explicit: it runs **BYPASSRLS** (isolation and RLS are
not exercised), writes **no** audit rows, performs **no** validation, is invisible to the contract
gate, and breaks on schema changes without warning. It stays documented as a bridge, never as the
target design.
