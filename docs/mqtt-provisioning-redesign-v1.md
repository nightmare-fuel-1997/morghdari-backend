# MQTT-Native Device Provisioning — Analysis, Architecture & Migration Plan

**Version:** 1.0 (design only — no code changed)
**Date:** 2026-10-18
**Status:** Awaiting owner approval before Phase 4 (implementation)
**Scope:** Gateway identity, installation workflow, sensor binding, and the
backend→gateway control channel, replacing the gateway's HTTP dependency with MQTT.

> **How to read this document.** Every claim about the current system carries a
> `path:line` reference. Claims are tagged with one of three states, and the
> distinction is load-bearing — the single biggest risk in this project is
> treating a documented contract as an implemented capability:
>
> - **[IMPL]** — implemented and reachable in running code.
> - **[CONTRACT]** — specified in the docs corpus / protocol contract, but with
>   no executing code path.
> - **[MISSING]** — not designed and not implemented; must be built.

---

## 1. Executive summary

### 1.1 Verdict on the proposed business logic

The proposed flow is directionally right and the security instincts behind it
(no plaintext passwords, command correlation, sensor admission control) are
correct. Four parts of it need adjustment before implementation, and one
assumption is factually wrong about the current system:

| # | Your proposal | Verdict |
|---|---|---|
| 1 | Hardware team defines a username/password per gateway and stores it in flash | **Keep, but invert who mints what.** The password must be generated server-side and returned once (already the case); the *username* must be the permanent slot id assigned at the factory. |
| 2 | Backend stores the password "using Django's Password Hasher" | **Do not.** Django's hashers are for human logins. Device credentials must be EMQX-native bcrypt — already implemented correctly. Mixing them silently breaks broker auth. |
| 3 | Provisioning commands (`add_dev` etc.) pushed over MQTT | **Correct goal, wrong primitive.** A fire-and-forget command cannot express "the gateway should now know X". Use retained desired state + reported state, with commands reserved for imperative actions. |
| 4 | `add_dev` may already exist — verify | **Verified: it does not exist anywhere in the codebase.** It exists only in two documentation files. There is no backend MQTT publisher of any kind. |
| 5 | Installer assigns sensors; gateway must distinguish authorized sensors | **Keep, and strengthen.** The real gap is that sensor *slots* cannot be created through any API at all today. |

### 1.2 The one structural finding

**The backend has no outbound MQTT capability.** Not a partial one, not a stub —
none. `paho-mqtt` is a declared dependency (`backend/requirements/base.txt:11`)
and is imported in exactly one file, the *inbound* subscriber
(`backend/morghdari/subscriber/main.py:30`). No module under `backend/apps/`
imports it, and no code publishes to any topic.

Everything downstream of that fact follows:

- Topics T3 (`commands`), T4 (`commands/response`), T5 (`clock/sync`), T6
  (`ota`), T7 (`ota/response`), T8/T9 (diagnostics) are **[CONTRACT]** only.
- `add_dev` / `set_label` are **[CONTRACT]** only.
- The `django-backend` broker principal documented in
  `MQTT Topic Map & EMQX Config.md` §3.3 has never been seeded — the only
  seeded service accounts are the two subscribers
  (`backend/apps/mqtt_auth/management/commands/seed_subscriber_credentials.py:88`).
- The subscriber does not even subscribe to `commands/response`
  (`backend/morghdari/subscriber/main.py:41-42` subscribes to telemetry and
  status only), so even a hand-published ACK would be discarded.

So this is not a migration of an existing MQTT command channel. It is the
**first construction** of one, plus a re-homing of the config-delivery path
from HTTP to MQTT. That is a smaller job than "rewrite provisioning" and a
larger job than "wire up `add_dev`".

### 1.3 What we reuse (and must not rebuild)

The existing provisioning core is healthy and should be kept nearly intact:

- The **state machine and audit trail** — `transition()`
  (`backend/apps/provisioning/services.py:57`) and the guarded matrix
  (`:98`). Every status change already lands in `provisioning_audit_log`.
- The **trust boundary** — pre-approval telemetry held in
  `telemetry:pending_approval`, drained on approval, archived on rejection
  (`backend/morghdari/subscriber/pipeline.py:30`,
  `backend/apps/provisioning/services.py:108-179`).
- The **credential model** — bcrypt `$2b$` in
  `mqtt_device_credentials` (`backend/apps/mqtt_auth/models.py:18`), staged
  rotation with commit-on-ACK (`backend/apps/mqtt_auth/services.py:365-405`).
- The **ACL generator** — `gateway_acl_rule_items()`
  (`backend/apps/provisioning/api.py:63`) already emits the full T1–T9 set.
- The **broker wiring** — EMQX postgres authn/authz with `no_match=deny`
  (`emqx/emqx.conf:86-130`) and the `emqx_auth` RLS exemption
  (`backend/apps/tenants/migrations/0003_emqx_auth_read_policy.py`).
- The **topology models** — slot / zone / position / sensor-slot / mapping
  (`backend/apps/devices/models.py:45-214`), which already model exactly the
  hierarchy the installer needs.

### 1.4 Prior-art audit — was this predicted?

Yes, substantially. A corpus sweep of `backend_docs` after the first draft of
this document found that most of the "missing" command channel was **specified
in detail and then silently dropped**. This matters because it changes the
work from *design* to *implement the existing design*, which is cheaper and
carries less review risk.

| Capability | Where it was specified | What happened |
|---|---|---|
| Publisher client | `versions/v0.5-provisioning-zero-touch/04-step-by-step-implementation-tasks.md:65-67` — **E1**, exact path `apps/mqtt_auth/publisher.py`, paho, `django-backend` credential, TLS, QoS 1, helpers to T3/T5/T6/T8 | Never written. `apps/mqtt_auth/publisher.py` does not exist. |
| `send_command()` + full lifecycle | Same file `:69-71` — **E2**: `command_logs` INSERT (SENT), `SETEX cmd:timeout:{uuid}`, publish envelope, response consumer, timeout task, late-SUCCESS handling | Never written. No `send_command` anywhere. |
| Command state machine | `Implementation Blueprint — How to Build.md:331-338` — **§11.2**, a complete SENT→ACKNOWLEDGED→SUCCESS→FAILED matrix including the late-SUCCESS rule | Documented only. `command_logs` has zero writers. |
| Rotation beat + commit-on-device-ACK | v0.5 `:73-79` — **E3/E4**: daily 02:00 staging, replay deferral, commit inside the command-response consumer | Half-built: staging and commit exist as **REST-triggered** functions (`mqtt_auth/services.py:365-405`) reachable only from `POST /rotation-ack/` (`provisioning/api.py:1174`). The device-mediated path does not exist. |
| `add_dev` / `set_label` / `REFRESH_CONFIG` senders | v0.5 `:85-87` — **E6** | Never written. |
| `django-backend` broker principal | `MQTT Topic Map` §3.3 (`:207-216`), `Security Architecture.md:350`, secrets table | Documented with an explicit accepted-risk note (`Open Items Tracker.md:160`, re-affirmed `:275`). Never seeded. |
| Command-response subscriber lane | Implied by E2's "response consumer"; ACL row specified at `MQTT Topic Map` §3.2 | Not subscribed (`main.py:41-42`). |
| v0.5 acceptance tests for all of the above | v0.5 `05-testing-and-acceptance-criteria.md:66-76,85` — `test_command_timeout_fails_row`, `test_late_success_stays_failed`, `test_rotation_commit_on_success`, `test_rotation_end_to_end_with_simulator` | **None of these tests exist** in `backend/tests/` (verified by name). |

The drop is traceable to one line in the tracker
(`Open Items Tracker.md:371`):

> "M5-01..M5-09 CLOSED … **Deferred: rotation command-envelope
> discrimination → v0.6 command channel** (v0.5 ships the REST stub)"

But v0.6 is `v0.6-rest-websocket`, and its five version documents contain
**zero** occurrences of `command`, `publisher`, or `downlink` (verified by
grep). The deferral was aimed at a version that never picked it up, and no
later tracker entry re-homed it. That is how a fully-specified subsystem
disappears: not by a decision, but by a hand-off to a placeholder.

**Three contradictions were also found — these were *not* predicted, they were
disagreed upon and never resolved:**

| # | Contradiction | Consequence |
|---|---|---|
| X1 | `Actor Use-Case & Dependency Map.md:52` states the Hardware Team is "**not scoped to any org or farm**", and `:433` names the Hardware Team as the writer of `gateway_slots`. `Implementation Blueprint — How to Build.md:316` states the slot is inserted by "**gateway_request review APPROVED (platform side)**". | Two documents assign the same write to different actors. The code followed the Blueprint (`provisioning/api.py:524`), which is why the Hardware Team ended up inside an org-scoped support session (`accounts/views.py:710-732`) — an org-less actor forced into an org-scoped gate. **RESOLVED 2026-10-18:** the owner confirmed the Hardware Team is platform staff of the software company, so the Actor Map is correct and the Blueprint rule is superseded for factory registration. |
| X2 | `Saas.md:1192` ("Provisioning Handshake" step 1): "Hardware team logs the device's factory serial and token secret via the Django API. Status initialized as `PENDING_INSTALLATION`." | Never says which org. `gateway_slots.org_id` is NOT NULL, so the sentence is unimplementable as written. This is the exact seam your Phase 1 lands on. |
| X3 | `Actor Use-Case & Dependency Map.md:437` lists `saloon_sensor_slots` writers as "Hardware Team, **Device Installer (via provisioning sync)**, Farm Manager (pre-registration)". | `provisioning_sync` only *validates* existing sensor slots (`api.py:705-709`) and never creates one. The documented writer does not exist. |

**What was genuinely never designed** (so this document does contribute new
architecture, not just a rescue of old specs):

- An **org-less factory inventory** table. `device_units` is new. The closest
  prior art is `Actor Use-Case & Dependency Map.md:437`'s phrase
  "Farm Manager (pre-registration)", which still assumes an org.
- **Retained desired state / `config_version` convergence.** Retained messages
  appear in the corpus only for the LWT status topic
  (`MQTT Topic Map` §1.2, `:34`). Idempotency is specified everywhere as
  `command_uuid` only — no ordering, no "already newer than me" test.
- **A device-rooted control namespace.** §6 of the Topic Map mandates that
  every topic be org-prefixed; nothing anticipates a pre-org device.
- **Installer work assignment.** `gateway_requests` has no assignee column
  (`database.md:131-142`), and no document describes an installer queue.
  Province scoping exists (`account_role_scopes.province_id`, added
  2026-08-23) but it narrows visibility *within an org*; it does not route
  work to a person.
- **A sensor-slot creation endpoint.** No task in any version specifies one.

One further finding deserves emphasis: `Actor Use-Case & Dependency Map.md:100`
already carries a **"Gap:"** note saying the installer panel must validate that
no active `hardware_registry` row exists before binding a slot — which is
exactly defect D6. It was written down as a known gap and never closed.

**Net effect on the plan.** Phase 3 (command channel) shrinks from "design and
build" to "implement E1–E6 as written, then extend the envelope for
`PROVISION`". Phases 0–2 are unaffected — the org-less inventory and the
desired-state model have no prior art and remain genuinely new. §9 is
unchanged; the task count for Phase 3 drops by roughly half.

---

## 2. Method

Discovery proceeded bottom-up from executable code, then outward to the
contract documents, then to the OpenAPI surface. The order matters: the docs
corpus is unusually rich for this project, and several documents describe
capabilities that were planned and never shipped (v0.5 E6 is the clearest
example — see §3.8).

Evidence base:

- `backend/apps/{devices,mqtt_auth,provisioning,telemetry,accounts,farms,auditing,tenants}/`
- `backend/morghdari/subscriber/`
- `emqx/emqx.conf`, `initdb/{01_roles,02_rls}.sql`
- `M:\projects\poultry\backend_docs\{MQTT Topic Map & EMQX Config.md, Gateway Firmware Specification.md, design-zero-touch-provisioning.md, database.md, Security Architecture.md, Open Items Tracker.md}`
- `backend/openapi.yaml`, `backend/config/urls.py`

---

## 3. Current architecture (as built)

### 3.1 Actors and their real surfaces

| Actor | Intended surface | Actual surface | State |
|---|---|---|---|
| Hardware Team | Register device + credentials before any farm exists | Register a serial onto an **already-existing** slot, inside an **org-scoped support session** (`backend/apps/accounts/api_hardware.py:70`) | Partial — requires a slot that only a platform review can mint |
| Organization rep | Submit install request with farm info | `POST /gateway-requests/` with `farm_id` + free-form `details` JSON (`backend/apps/provisioning/api.py:417`) | **[IMPL]** but manager/owner only, and the farm must already exist |
| Platform support | Review request, approve/reject | `POST /gateway-requests/{id}/review/` (`:482`) | **[IMPL]** |
| Routing to installer by province | Assign request to the installer covering the province | — | **[MISSING]** |
| Installer | See assigned requests | — | **[MISSING]** |
| Installer | Enter gateway credentials, validate device | — | **[MISSING]** |
| Installer | Register halls | `POST /farms/{id}/saloons/` — **manager+ only** (`backend/apps/farms/api_follow.py:439`) | **[IMPL]** but wrong role |
| Installer | Add sensors to a hall | — no writer for `SaloonSensorSlot` exists anywhere | **[MISSING]** |
| Installer | Upload photos | `POST /saloons/{id}/photos/` — engineer+ (`backend/apps/monitoring/ops_views.py:571`) | **[IMPL]** but saloon-scoped only; no gateway/sensor photo |
| Gateway | Fetch config | `GET /gateways/{id}/config/` over HTTPS + Basic (`backend/apps/provisioning/api.py:1039`) | **[IMPL]** — the HTTP dependency to remove |
| Gateway | Receive commands | — | **[MISSING]** |
| Gateway | Report command result | — | **[MISSING]** |

### 3.2 Identity and credentials

```
accounts.User  ──(human login, Django hashers: argon2/pbkdf2/bcrypt_sha256)
mqtt_device_credentials ──(device auth, raw bcrypt $2b$)
```

Two separate credential systems, deliberately:

- `MqttDeviceCredential` (`backend/apps/mqtt_auth/models.py:18`):
  `device_id` **unique and equal to `gateway_slot_id`**, `username` unique,
  `password_hash`, `pending_password_hash`, `pending_since`, `is_active`,
  `last_rotated_at`, `expires_at`.
- Hashing is **raw bcrypt**, not Django's hasher:
  `hash_password()` (`backend/apps/mqtt_auth/services.py:49`) calls
  `bcrypt.hashpw(...)`. `verify_password()` (`:56`) additionally accepts
  legacy Django wrapper strings for backward compatibility.
- `PASSWORD_HASHERS` (`backend/config/settings/base.py:207`) is therefore
  **irrelevant to devices** and must stay that way. EMQX 5.5's
  `password_based:bcrypt` backend parses `$2b$` and nothing else: a
  `pbkdf2_sha256$…`, `argon2$…` or `bcrypt_sha256$…` string is rejected at
  CONNECT with no useful diagnostic.

> **Answering your question 2.4/2.5 directly.** The password *is* stored
> correctly (bcrypt, never plaintext, never logged). But "use Django's
> Password Hasher" would be a regression, not a hardening: Django's default
> hasher in this project is Argon2 (falling back to PBKDF2), neither of which
> EMQX can verify. The correct statement of the requirement is: *store device
> credentials with a hasher the broker can verify* — which is what the code
> does. The two systems must not be unified.

### 3.3 Broker authentication and authorization

Authentication (`emqx/emqx.conf:86-104`) is a single SQL query against
Postgres, executed by a dedicated read-only role:

```sql
SELECT cred.password_hash AS password_hash
FROM mqtt_device_credentials cred
JOIN gateway_slots gs ON gs.slot_id = cred.device_id
WHERE cred.username = ${username}
  AND cred.is_active = true
  AND gs.provisioning_status IN
      ('PENDING_INSTALLATION','PENDING_APPROVAL','PROVISIONED')
```

Three consequences that constrain the whole design:

1. **The JOIN is an identity gate.** A credential with no `gateway_slots` row
   can never authenticate. A factory-registered device therefore *must* have
   a slot row, or the query must be extended. (§5.2 resolves this.)
2. **`REJECTED` / `SUSPENDED` fail at CONNECT**, not just at authorization —
   the documented kill switch, implemented.
3. **`PENDING_*` may connect**, because their telemetry is buffered rather
   than dropped (Reconciliation R3).

Authorization (`:109-130`) reads `mqtt_acl_rules` by username with
`no_match = deny` and `deny_action = ignore`. Only `allow` rows are seeded —
blanket deny rows were deliberately removed (AU-fix 2026-08-16) to avoid
depending on EMQX's row-ordering semantics.

`gateway_slots` is `FORCE ROW LEVEL SECURITY`
(`initdb/02_rls.sql:77`), so the authn JOIN would return zero rows for
`emqx_auth` without the targeted exemption added in
`backend/apps/tenants/migrations/0003_emqx_auth_read_policy.py` — SELECT-only,
that role only, that table only.

### 3.4 Topic inventory — implemented vs contracted

| # | Topic | Direction | State |
|---|---|---|---|
| T1 | `{org}/farms/{farm}/gateways/{gw}/telemetry` | up | **[IMPL]** — regex at `backend/morghdari/subscriber/topics.py:17`, consumed via `$share/telemetry_workers/…` (`main.py:41`) |
| T2 | `…/status` | up + LWT | **[IMPL, BROKEN]** — subscribed (`main.py:42`) but routed into the telemetry parser, which rejects it → every status message is dead-lettered as `bad_topic`. See §3.8-D1. |
| T3 | `…/commands` | down | **[CONTRACT]** — no publisher, no consumer of its responses |
| T4 | `…/commands/response` | up | **[CONTRACT]** — subscriber does not subscribe |
| T5 | `…/clock/sync` | down | **[CONTRACT]** |
| T6 | `…/ota` | down | **[CONTRACT]** |
| T7 | `…/ota/response` | up | **[CONTRACT]** |
| T8 | `…/diagnostics/request` | down | **[CONTRACT]** |
| T9 | `…/diagnostics/response` | up | **[CONTRACT]** |

ACL rules for all nine are generated and written at approval time
(`backend/apps/provisioning/api.py:544`), and backfilled for pre-M8-04-H slots
by `hw_gateway_acl_backfill` (`backend/apps/accounts/api_hardware.py:412`).
So the *permissions* exist; the *traffic* does not.

### 3.5 Provisioning lifecycle as implemented

```
GatewayRequest.PENDING
   └─ review APPROVE  ──► GatewaySlot created (org+farm), credential minted,
                          T1–T9 ACL rows written, EMQX user pushed,
                          plaintext password returned ONCE
                          status: PENDING_INSTALLATION
   └─ review REJECT   ──► GatewayRequest.REJECTED

PENDING_INSTALLATION
   └─ installer POST /provisioning/sync/  ──► zones + pack mappings written
                                              status: PENDING_APPROVAL

PENDING_APPROVAL
   ├─ manager approve ──► PROVISIONED (+ drain telemetry:pending_approval)
   └─ manager reject  ──► REJECTED  (+ DLQ archive, ACL revoke)

PROVISIONED ── manager suspend ──► SUSPENDED (+ ACL rows deleted)
SUSPENDED   ── manager resume  ──► PROVISIONED (+ ACL rows regenerated)
```

References: `backend/apps/provisioning/api.py:482` (review),
`:602` (sync), `:793/:838/:880/:921` (lifecycle), and the guarded matrix at
`backend/apps/provisioning/services.py:98`.

Two properties worth preserving verbatim:

- `_transition_guarded` refuses illegal edges and the API maps that to 409
  **without** writing a status.
- Broker steps run **after** the DB commit, so a broker outage can never roll
  back a business decision ("FNV-soft"). Convergence is left to the next
  sweep or rotation.

### 3.6 Where the gateway gets its configuration today

`GET /api/v1/gateways/{gateway_slot_id}/config/`
(`backend/apps/provisioning/api.py:1039`) with HTTP Basic, username =
`gateway_slot_id`, password verified against the bcrypt hash
(`_basic_device`, `:954`). Payload from
`gateway_config_payload()` (`backend/apps/mqtt_auth/services.py:77`):

```json
{
  "gateway_slot_id": "GW-0041",
  "org_id": "…uuid…",
  "farm_id": 7,
  "provisioning_status": "PENDING_INSTALLATION",
  "mqtt": {"host": "...", "port": 8883, "tls": true},
  "auth": {"username": "GW-0041", "password_hash": "$2b$…",
           "password_issued_once": true, "rotation_pending": false},
  "rate_limit_note": "..."
}
```

Note what is **deliberately absent**: any sensor slot ids (R2 — the gateway
must not cache mappings that go stale) and the plaintext password. This
payload shape is the right contract; the redesign changes its *transport*, not
its content. That is the core of the low-cost migration argument.

### 3.7 The command channel: what actually exists

| Piece | State |
|---|---|
| Backend MQTT client | **[MISSING]** |
| `django-backend` broker principal + ACL | **[CONTRACT]** (`MQTT Topic Map` §3.3), never seeded |
| Command journal / correlation table | **[MISSING]** — see below |
| `add_dev` / `set_label` sender | **[CONTRACT]** — planned as v0.5 task E6 (`versions/v0.5-provisioning-zero-touch/04-step-by-step-implementation-tasks.md:85`), never shipped |
| Command response consumer | **[MISSING]** |
| Retry / TTL / idempotency | **[MISSING]** |

There is one near-miss worth naming: `CommandLog`
(`backend/apps/auditing/models.py:15`, table `command_logs`, already
RLS-enrolled at `initdb/02_rls.sql:69`). It looks like a command journal and
is **dead code** — zero writers and zero readers anywhere in `apps/` or
`tests/`. It is also structurally unusable for this purpose:

- `saloon_id` is `NOT NULL` — gateway-level provisioning has no saloon;
- `actuator_type` is `CHECK IN ('heater','exhaust_fan','curtain')` — no room
  for `PROVISION` / `ROTATE_CREDENTIAL` / `DIAGNOSTICS`;
- statuses are `SENT|ACKNOWLEDGED|SUCCESS|FAILED` — no `PENDING`, no
  `EXPIRED`, no `SUPERSEDED`, no attempt counter, no idempotency key.

Widening it would mean a destructive migration on a table that also carries
`legal_hold` retention semantics (DR Plan §5). **Do not reuse it.** Add a
purpose-built table (§5.4) and leave `command_logs` for its Phase-2 actuator
purpose.

> **Note on prior art (see §1.4).** The v0.5 plan intended `command_logs` to be
> the journal — E2 says "`command_logs` INSERT (SENT)"
> (`versions/v0.5-provisioning-zero-touch/04-step-by-step-implementation-tasks.md:70`)
> and Blueprint §11.2 titles its matrix "Command lifecycle
> (`command_logs.current_status`)". I am overriding that choice, not overlooking
> it: the table's `saloon_id NOT NULL` and actuator-only CHECK make it
> unusable for gateway-level provisioning, and relaxing them would weaken the
> Phase-2 actuator ledger that `legal_hold` depends on. The state *matrix* from
> Blueprint §11.2 is reused verbatim — only the storage changes.

### 3.8 Confirmed defects found during discovery

These are not opinions; each is reproducible from the cited line.

| ID | Defect | Evidence | Impact on this project |
|---|---|---|---|
| **D1** | Gateway status/LWT messages are dead-lettered, never processed | `main.py:299-315` routes *every* message through `handle_message`, whose gate 1 rejects anything not ending in `/telemetry` (`topics.py:17`). `_handle_status` (`main.py:371`) is defined and **never called** (grep: single occurrence). | No gateway online/offline state exists. Blocks "device was offline when the command was issued" handling. |
| **D2** | `_handle_status` would also fail if wired | It looks up `SaloonSensorSlot` by the gateway segment (`main.py:391`), but that segment is a `gateway_slot_id` (`GW-…`), not a sensor slot id. | Fixing D1 requires fixing this too. |
| **D3** | ACL topics are built from `slot.farm_id`, which is nullable | `gateway_acl_rule_items(str(org), slot.farm_id, slot.slot_id)` (`api.py:544`, `services.py:155`) f-stringifies `farm_id`; a farm-less slot yields `…/farms/None/…`. | A factory-registered, not-yet-bound device would get rules that match nothing. |
| **D4** | ACLs are not regenerated when a slot's farm changes | Written once at approval; regenerated only on resume (`services.py:147`). | Rebinding a device to another farm silently breaks its topics. |
| **D5** | No sensor-slot write path | No `SaloonSensorSlot.objects.create` anywhere in `apps/`; `provisioning_sync` only *validates* existing slot ids (`api.py:705-709`). | The installer's "add sensors" step has nowhere to write. |
| **D6** | A physical sensor serial can be registered to two slots | `HardwareRegistry.hardware_id` has no unique constraint — only a partial index on `slot` (`devices/models.py:166-174`). | Directly violates your "sensor must not be assigned to two gateways" requirement. |
| **D7** | Installer cannot create halls | `saloon_create` requires `MANAGER_ROLES` (`farms/api_follow.py:439`). | Phase 3 of the requested flow is blocked for the installer actor. |
| **D8** | `_support_op_context` requires an org-scoped session | `accounts/views.py:710-732` returns 403 without an `org_id` claim. | Factory-time registration (no org yet) cannot use the existing hardware endpoints. |
| **D9** | Rotation commit is human-mediated, not device-mediated | `commit_pending_rotation` is only reachable from `POST /rotation-ack/` with the plaintext (`api.py:1174`), whereas `MQTT Topic Map` §1.3.3 specifies the device publishes `SUCCESS` and the backend commits. | The documented MQTT rotation dance does not exist; it must be built or the doc corrected. |

D1–D2 are new findings not present in `BUGS.md`; they should be filed there
regardless of this project's outcome.

---

## 4. Gap analysis

| # | Requirement | Current state | Gap |
|---|---|---|---|
| G1 | Device registered with identity before any farm/org exists | Slot can only be minted by a platform review that requires org+farm | **Model**: no org-less device entity |
| G2 | Credentials issued at the factory, stored in flash | Password generated server-side at approval and shown once — workable, but only after a farm is chosen | **Sequencing** |
| G3 | Password hashed, broker-verifiable | bcrypt `$2b$` in `mqtt_device_credentials` | **None** ✔ |
| G4 | Org submits request with location/requirements/expected equipment | `details` JSON is free-form and unvalidated; `farm_id` must already exist | **Schema**: no structured install spec |
| G5 | Support approves, assigns to installer by province/city | Review exists; no assignee, no routing, no queue | **Missing entity + endpoints** |
| G6 | Installer sees assigned work | — | **Missing endpoint** |
| G7 | Installer validates a device by entering username+password | `provisioning_sync` assumes the slot is already bound to the caller's org | **Missing endpoint** |
| G8 | Installer registers halls | Exists, manager-only | **Role widening** |
| G9 | Installer adds sensors and assigns to halls | No sensor-slot writer | **Missing entity write path** |
| G10 | Gateway distinguishes authorized sensors from foreign ones | `add_dev` contract only; no sender, no ACK, no version | **Missing protocol + code** |
| G11 | Backend pushes sensor config to gateway | No publisher | **Missing subsystem** |
| G12 | Gateway has zero HTTP dependency | HTTP config fetch is the *primary* path (`Firmware Spec §2 step 7`) | **Transport re-homing** |
| G13 | Command correlation / idempotency / retry | `CommandLog` dead and unusable | **Missing table + worker** |
| G14 | Offline device when config changes | HTTP: unreachable. MQTT: retained state not designed | **Missing convergence model** |
| G15 | Sensor bound to at most one gateway | No uniqueness on active `hardware_id` | **Missing constraint** |
| G16 | DB / publish / apply distinguished | HTTP returns 200 with no device confirmation | **Missing state machine** |

---

## 5. Proposed architecture

### 5.1 Design principles

1. **Reuse the contract, change the transport.** The config payload already
   carries exactly what the device needs (`org_id`, `farm_id`, broker
   settings). We move delivery from HTTPS-pull to MQTT-retained-push and keep
   the field names.
2. **Convergence beats delivery.** Provisioning is not a command, it is a
   desired state the device must reach. Model it as state, not as an event,
   and every offline/duplicate/retry problem collapses into "is reported ==
   desired?".
3. **Never let one transaction own two systems.** DB writes commit first;
   broker work is enqueued and retried. This is already the project's FNV-soft
   convention (`services.py:108-125`) and it is correct.
4. **The ACL is the security boundary, not the payload.** A device must be
   physically incapable of touching another tenant's topics, whatever a
   compromised backend or buggy firmware sends.
5. **Additive before subtractive.** Every migration phase must leave the
   previous path working, behind a config flag (`system_configuration`,
   the project's existing config-is-data convention, D21).

### 5.2 Identity model — the one new table

The blocker for G1 is structural: `GatewaySlot.org` is `NOT NULL`
(`devices/models.py:47`) and `gateway_slots` is `FORCE ROW LEVEL SECURITY`, so
an org-less slot is both impossible and invisible. Three options were
considered.

| Option | Verdict |
|---|---|
| Make `GatewaySlot.org` nullable | **Rejected.** Breaks the RLS equality policy's `WITH CHECK`, forces every hardware-team write onto the `BYPASSRLS` admin alias, and invalidates the invariant dozens of code paths rely on ("a slot always belongs to an org"). High blast radius for zero gain. |
| Park unbound devices in a synthetic "Platform" org | **Rejected.** Pollutes a real org's namespace, makes binding a cross-tenant move that needs the admin alias, and leaves orphan rows in tenant tables that every org-scoped report must learn to exclude. |
| **Dedicated org-less factory registry** | **Chosen.** Mirrors an existing, proven posture in this codebase: `mqtt_device_credentials` and `mqtt_acl_rules` are already org-less precisely because the broker reads them. |

**`device_units`** — factory inventory. Org-less by design, therefore no RLS
policy (consistent with the documented rule that org-less globals carry no
policy, `initdb/02_rls.sql:24-26`).

> **Owner confirmation 2026-10-18:** the Hardware Team belongs to the software
> company itself, not to any customer organization. This settles corpus
> contradiction X1 (§1.4) in favour of the Actor Map: the team really is
> org-less, so an org-less inventory table is the correct model and the
> Blueprint's "platform review inserts the slot" rule does not apply to factory
> registration. It also means the factory endpoints must be gated by
> `is_platform_staff` + `staff_role`, **not** by `_support_op_context`
> (`accounts/views.py:710`), which requires an org-scoped session.

```
device_units
  unit_id      VARCHAR(100) PK     -- factory serial; == the MQTT username;
                                   -- becomes gateway_slots.slot_id at binding
  hardware_id  VARCHAR(100) UNIQUE -- MAC / chip id printed on the label
  claim_code_hash VARCHAR(255)     -- bcrypt hash of the single-use claim code
                                   -- printed in the QR; consumed at binding
  claimed_at   TIMESTAMPTZ NULL
  model        VARCHAR(50)  NULL
  fw_version   VARCHAR(50)  NULL
  batch        VARCHAR(50)  NULL
  state        VARCHAR(20)  CHECK IN ('REGISTERED','BOUND','RETIRED')
  slot_id      VARCHAR(100) NULL FK gateway_slots(slot_id)  -- set at binding
  created_by   INT NULL FK accounts_user
  created_at / bound_at / retired_at
  notes        TEXT NULL
```

**The QR code carries two values: `unit_id` and `claim_code`.** The claim code
is the possession proof (AD-18). It is single-use, stored only as a hash, and
is *not* the MQTT password — so a photographed label cannot be used to
authenticate as the device, and a leaked claim code cannot break broker auth.

**The pivotal decision: `unit_id` == the eventual `slot_id`.** The hardware
team assigns the permanent logical identity at the factory, which is exactly
what the firmware spec already promises ("credentials (`gateway_slot_id` +
password) are pre-loaded at factory into encrypted flash",
`Gateway Firmware Specification.md` §2 step 5). Consequences:

- The device's username never changes across its entire life. No reflash on
  binding, no credential reissue, no re-pairing.
- `mqtt_device_credentials.device_id` is set once, at the factory.
- Binding is a **pure server-side link**: create the `GatewaySlot` with
  `slot_id = unit_id`, attach `org`/`farm`, flip `state` to `BOUND`.
- The authn JOIN keeps working with no schema change to its shape — only the
  `WHERE` clause gains an org-less branch (§7.1).

### 5.3 Three-layer state model

Your §5 requirement — distinguish *stored in DB* / *published* / *applied* —
is promoted to the core abstraction. Three layers, three tables/columns:

| Layer | Where it lives | Meaning |
|---|---|---|
| **Desired** | `gateway_slots.desired_config_version` + the retained payload on `devices/{slot}/desired` | What the gateway *should* know |
| **Reported** | `gateway_slots.applied_config_version` (+ `applied_at`) | What the gateway *says* it knows |
| **Command** | `gateway_commands` rows | Imperative one-shots (rotate, diagnostics, reboot) with their own lifecycle |

Derived installation state, computed, never stored as an enum:

```
applied == desired  →  APPLIED
applied <  desired  →  PENDING  (published, not yet confirmed)
published failed    →  FAILED   (last_publish_error set, retry scheduled)
```

No new value is added to the `provisioning_status` CHECK constraint. That is
deliberate: the constraint is a lifecycle matrix with guarded edges, and
mixing transport state into it would make the matrix ambiguous. `PENDING` /
`APPLIED` are *config convergence* states, orthogonal to
`PENDING_INSTALLATION` → `PROVISIONED`.

**The four config states, as the frontend must show them (AD-22).** The owner
asked for a way to see which devices have not received their settings. It is
derived, never stored — one endpoint, `GET /provisioning/config-state/`:

| State | Condition | What the operator does |
|---|---|---|
| `APPLIED` | `applied == desired` | nothing |
| `PENDING` | `applied < desired` **and** the last publish succeeded within `config_apply_timeout_sec` | wait |
| `STALLED` | `applied < desired` **and** the last publish succeeded but the timeout elapsed | the device is not consuming — check connectivity, power, broker ACL |
| `FAILED` | the last publish failed, or the device reported `FAILED` with an error | fix the cause; the retry is explicit |

Two extra columns make the view actionable: `last_publish_error` and
`last_reported_at`. A device that has *never* reported is `STALLED` from the
moment its job is submitted, which is exactly the "installed but never came
online" case that otherwise goes unnoticed until someone complains.

### 5.4 Components

```
┌──────────────┐  HTTPS/JWT   ┌────────────────────┐
│ Panels       │─────────────►│ Django (daphne)    │
│ HW/Support/  │              │  provisioning app  │
│ Installer    │              └─────────┬──────────┘
└──────────────┘                        │ DB commit first
                                        ▼
                              ┌────────────────────┐
                              │ gateway_commands   │  PENDING
                              │ desired_version++  │
                              └─────────┬──────────┘
                                        │ Celery enqueue
                                        ▼
                              ┌────────────────────┐   MQTT publish (QoS1)
                              │ Celery: publisher  │──────────────────┐
                              │ apps/.../publisher │                  │
                              └─────────┬──────────┘                  ▼
                                        │ SENT/FAILED        ┌─────────────┐
                                        │                    │    EMQX     │
                                        │                    │ postgres    │
                                        │                    │ authn/authz │
                                        │                    └──┬───────┬──┘
                                        │        retained D1    │       │ D2
                                        │                       ▼       │
                                        │                 ┌──────────┐  │
                                        │                 │ Gateway  │──┘
                                        │                 └──────────┘
                                        │  reported (D2)         │
                                        ▼                        │
                              ┌────────────────────┐             │
                              │ Subscriber (N≥2)   │◄────────────┘
                              │ $share/…/reported  │
                              └─────────┬──────────┘
                                        │ Redis stream
                                        ▼
                              ┌────────────────────┐
                              │ Celery: reconciler │ → applied_version, command state
                              └────────────────────┘
```

New pieces: `device_units` (table), `gateway_commands` (table), a publisher
module + Celery tasks, two subscriber lanes, two topics. Everything else is
existing machinery.

### 5.5 Decisions and rejected alternatives

| # | Decision | Rejected alternative | Why |
|---|---|---|---|
| AD-1 | Org-less `device_units` registry | Nullable `GatewaySlot.org` | Preserves the RLS invariant; no privileged alias needed for inventory writes |
| AD-2 | `unit_id` == `slot_id` at the factory | Mint a new slot id at binding | Device never reflashes; credential never reissues; binding is one UPDATE |
| AD-3 | Retained **desired state** on a stable per-device topic | Fire-and-forget command with retries | Offline, duplicate, and resync problems disappear; no queue-expiry cliff |
| AD-4 | `config_version` integer for idempotency | `command_uuid` only | Ordered and comparable; a device can decide "already newer than me" without a uuid ring buffer |
| AD-5 | New `gateway_commands` table | Extend `CommandLog` | `CommandLog` is saloon-scoped, actuator-only, dead, and carries `legal_hold` retention semantics |
| AD-6 | paho publish from Celery | EMQX HTTP `/api/v5/publish` | The HTTP API is an admin path that **bypasses per-topic ACL** — exactly the control we are strengthening. Keep it as a documented ops fallback only. |
| AD-7 | Firmware owns the topic *grammar*; backend supplies only `org_id`/`farm_id` | Backend sends literal topic strings | Deterministic, validatable on-device, and a compromised backend cannot aim a device at an arbitrary topic |
| AD-8 | Widen `installer` role for hall creation, gated by `AccountRoleScope` | New installer-specific endpoints | Reuses the existing scope-narrowing engine (`accounts/permissions.py:76`) |
| AD-9 | Sensor admission enforced on the gateway *and* in `GatewaySensorMapping` | Gateway-only | Defence in depth; a compromised gateway must still not be able to bind a foreign sensor server-side |
| AD-10 | Fix the status lane (D1/D2) as a prerequisite | Ignore, use reported state only | LWT is the only signal for a device that dies without disconnecting cleanly |
| AD-11 | **Support assigns a specific installer by hand** after approving the request (owner decision 2026-10-18) | Automatic province/city routing engine | A routing engine needs a coverage map, conflict rules, and a fallback for uncovered provinces — none of which exist. Manual assignment is one FK and one endpoint, and support already reviews every request. Province scope stays useful as a *filter* on the picker, not as an assignment algorithm. |
| AD-12 | Assignment lives on `gateway_requests` (one request = one installation job) | Separate `installation_jobs` table | **SUPERSEDED by A16** (owner: one request may cover several gateways) |
| AD-13 | Hardware Team is platform staff of the software company (owner 2026-10-18) | Treating them as tenant staff | Confirms `device_units` is org-less; factory endpoints gate on `is_platform_staff` + `staff_role`, never `_support_op_context`. Resolves corpus contradiction X1 in favour of the Actor Map. |
| AD-14 | **Installers are platform staff too**, and reach tenant data through a job-scoped session | Giving them an `OrgMember` row in every customer org | Owner: "the installer is a member of our organization". Reuses the existing audited support-session machinery instead of inventing a cross-org membership model (§5.6). |
| AD-15 | **The install request creates the farm** (owner 2026-10-18) | Requiring the farm to pre-exist | `gateway_requests.farm_id` becomes nullable plus `farm_spec`; approval materialises the `Farm` owned by the requesting org. Matches the brief's "they enter the farm's initial information". |
| AD-16 | **One request → N installation jobs**, one per gateway (owner 2026-10-18) | One job per request | `Saas.md` already says a gateway owns a zone of saloons, not a whole farm, so multi-gateway farms are normal. New `installation_jobs` table (`kind`, `request`, `org`, `farm`, `assigned_installer`, `state`, `gateway_slot`, `unit`); each job carries its own assignee, device, and slot. |
| AD-17 | **Remove farm-manager approval** (owner 2026-10-18); replace it with a technical gate | Keeping the D18 human gate | Two human gates already exist (org requests, support approves). The D18 gate existed to stop an installer self-approving; installers are now our own staff, so it oversees employees rather than protecting the customer. Replacement: the slot reaches `PROVISIONED` automatically when `applied_config_version == desired_config_version` **and** the installer submitted. No unconfigured device can go live. |
| AD-18 | **Bind by scanning a QR claim code; no password, no device-side interaction** | Installer enters username + password, or the device blinks an LED | Owner: no LED can be assumed ("how the device works isn't important"). A single-use `claim_code` printed in the QR proves possession of the physical label, needs no firmware behaviour, and works with the unit offline. The MQTT password never leaves the factory. |
| AD-19 | **Category A device-management commands move to `devices/{slot}/command`**; T3/T4 stay for Category B actuators | Leaving both on T3 | Category A must work **before** the device has an org (rotation and diagnostics on an unbound unit), and T3's topic is org-prefixed. The Topic Map already separates the two categories semantically; this makes the separation physical. Costs nothing today — T3 has no publisher. |
| AD-20 | **Per-device photos**: one `device_photos` table targeting either a gateway slot or a sensor slot | Reusing `SaloonPhoto` | Owner: each sensor is an independent entity and needs its own photo. `SaloonPhoto` is saloon-scoped only (`devices/models.py:216`). |
| AD-21 | **Reassignment is allowed at any time, and the same job machinery covers repair and maintenance** (owner 2026-10-18) | Install-only jobs; reassignment only before bind | Owner: "assigning the device installation and maintenance and repair process to another installer". `installation_jobs.kind ∈ {INSTALL, REPAIR, MAINTENANCE}`; only `INSTALL` is implemented in Phase 2, but the table is shaped once. |
| AD-22 | **A first-class "which devices have not received their configuration" view** (owner 2026-10-18) | Leaving it to log-grepping | Owner explicitly asked for it, visible in the frontend. Derived from `desired_config_version` vs `applied_config_version` plus publish state; one endpoint, four states. |
| AD-23 | **`pack_id` (the 8-hex node id) is mandatory on every sensor mapping and is printed on the sensor** | Treating it as optional metadata | Without it the gateway cannot filter LoRa traffic and the backend cannot resolve telemetry → every reading dead-letters as `UNKNOWN_SLOT`. It is burned at manufacture and read from the label; the installer never invents it. |

### 5.6 Access model — platform-staff installers (the biggest new piece)

Owner answer Q2 makes installers employees of the software company, like the
Hardware Team. That has three mechanical consequences:

1. They have **no `OrgMember` row** in the customer's org, so the JWT carries no
   `org_id`.
2. The tenant middleware's branch ② sends platform staff through **with no GUC**
   (`tenants/middleware.py:70-76`), so every tenant table returns zero rows by
   design.
3. Every existing installer check reads `"installer" in claims["roles"]`
   (`provisioning/api.py:611`), which comes from `OrgMember` — so it fails.

**Reuse rather than invent.** The project already solves "platform staff needs
scoped access to one org": the audited support session
(`accounts/views.py:550-615`). It issues a 15-minute token carrying
`org_id` + `support_session` + `incident_id`, middleware branch ① turns it into
an ordinary RLS-scoped user of exactly that org, and every open writes an audit
row. That is precisely the shape a job-scoped installer session needs.

**But do not reuse the `support_session` claim itself.** Holding it also opens
`_support_op_context` (`accounts/views.py:710`), which unlocks gateway
suspend/resume and alert force-resolve. An installer must not inherit a support
operator's powers.

**Design — a sibling `installer_session`:**

```
POST /provisioning/installations/{job_id}/session/
  gate: is_platform_staff AND NOT superuser
        AND job.assigned_installer == request.user
        AND job.state IN (ASSIGNED, BOUND, SUBMITTED)
  → 15-min access token with claims:
        org_id            (from the job)
        installer_session true
        job_id
        farm_id           (from the job)
  → audit row INSTALLER_JOB_ENTER {job_id, farm_id}
```

- **Middleware** gains branch ①b between ① and ②:
  `installer_session is True and org_id present` → set the GUC, exactly like ①.
- **`_support_op_context` is untouched** and keeps requiring `support_session`,
  so an installer token cannot reach the support-op surface.
- **New helper `installer_scope(request)`** → `(org_id, job_id, farm_id)`.
  Every installer endpoint filters by `farm_id` **explicitly**. This is not
  optional: `scoped_farms()` treats *zero* `AccountRoleScope` rows as
  "unnarrowed" (`accounts/permissions.py:76`), so a platform-staff installer
  would otherwise see every farm in the org.
- **`provisioning/api.py:611`** must accept the `installer_session` claim as an
  alternative to the `installer` org role. Keep the org-role path for backward
  compatibility; the new flow never creates it.

**Costs and risks.**

| Concern | Handling |
|---|---|
| A 15-minute token is short for a site visit | The client re-opens the session silently on 401; the job is still assigned, so re-issue is automatic. |
| Platform staff reading tenant data is the highest-risk surface in the system | Same posture as support sessions: 15-min expiry, per-job, audited, RLS-scoped, no BYPASSRLS. Add the enforced-RLS test that already exists for sweeps (`test_sweep_rls_enforced.py` pattern). |
| Two session types could drift | One shared issuer function with a `kind` argument; one middleware branch handling both claims; a test asserting a support token cannot open a job session and vice versa. |

---

## 6. MQTT design

### 6.1 Topic map — additions only

T1–T9 keep their exact current shape. Two topics are added:

| # | Topic | QoS | Retain | Direction | Purpose |
|---|---|---|---|---|---|
| **D1** | `devices/{slot_id}/desired` | 1 | **yes** | backend → gateway | Desired configuration document |
| **D2** | `devices/{slot_id}/reported` | 1 | no | gateway → backend | Config apply report, command ACKs, heartbeat |
| **D3** | `devices/{slot_id}/command` | 1 | no | backend → gateway | **Category A** device-management commands (identify, rotate, diagnostics, reboot) — works before the device has an org |

**Why a device-rooted namespace instead of a tenant-rooted one.** The obvious
alternative is `{org}/farms/{farm}/gateways/{gw}/config` (retained), which
would honour the "org prefixes every topic" rule
(`MQTT Topic Map` §6). It fails on the exact case we are solving: an unbound
device has no `org_id`, so it cannot compute its own topic, so it cannot
subscribe — a chicken-and-egg. And a tenant-rooted control topic must be
re-subscribed (plus ACL-rewritten) on every farm move, stranding the device in
the interval.

`devices/{slot_id}/…` is rooted at the identity the device *always* has. The
"org prefixes every topic" rule exists so the backend can treat the topic's
first segment as unforgeable org evidence for **ingested data**
(`topics.py:3-8`). D1/D2 carry no telemetry, so the rule's purpose is not
violated; the ACL compensates by pinning each username to one literal topic
(no wildcards). This is a deliberate, documented exception — record it in
`MQTT Topic Map` §6 as an amendment.

`T3 commands` / `T4 commands/response` remain for imperative actions and keep
their tenant-prefixed shape.

### 6.2 Payloads

**D1 — desired state (retained).** One document, complete and self-sufficient.
The device must be able to boot from this alone after a flash wipe.

```json
{
  "schema_version": "1.0",
  "config_version": 7,
  "slot_id": "GW-2026-0042",
  "org_id": "9f8c2d34-6f2b-4c9d-9d5e-2b7f4a1c8e30",
  "farm_id": 12,
  "farm_name": "Golden Egg North",
  "gateway_hardware_id": "AA:BB:CC:DD:EE:FF",
  "mqtt": { "host": "mqtt.morghdari.com", "port": 8883, "tls": true },
  "sensors": [
    {
      "pack_id": "a1b2c3d4",
      "slot_id": "SS-01",
      "saloon_id": 3,
      "saloon_name": "Saloon 1",
      "position": "North Wall",
      "label": "Saloon 1 - Temperature",
      "metrics": ["temp", "humidity"]
    }
  ],
  "report_interval_sec": 60,
  "server_ts": "2026-10-18T08:41:00Z"
}
```

Field notes:

- `sensors[].metrics` is sourced from `SaloonSensorSlot.allowed_metric_keys`
  (`devices/models.py:147`) — an existing column that answers your "what
  information should it receive from the sensors" requirement. It is
  currently written by nobody; this design gives it a writer and a consumer.
- `slot_id` is included for **self-check only**. The gateway must verify it
  equals its own identity and refuse the document otherwise — this is what
  makes a mis-addressed retained message harmless rather than dangerous.
- `sensors` is the **complete authorized set**, not a delta. Replacement
  semantics, like `add_dev`, but versioned and acknowledged.

**D2 — reported state (uplink).**

```json
{
  "schema_version": "1.0",
  "slot_id": "GW-2026-0042",
  "config_version": 7,
  "state": "APPLIED",
  "error": null,
  "sensor_count": 4,
  "rejected_packs": ["deadbeef"],
  "uptime_sec": 3600,
  "ts": "2026-10-18T08:41:03Z"
}
```

`state ∈ {APPLIED, FAILED, REJECTED}`. `REJECTED` = the document failed
validation (bad org/farm shape, self-id mismatch) and was not applied.

**D3 — Category A command (non-retained).**

```json
{
  "command_uuid": "b1f0…",
  "action": "DIAGNOSTICS_REQUEST",
  "params": {},
  "expires_at": "2026-10-18T08:46:00Z"
}
```

Actions in scope: `ROTATE_CREDENTIAL`, `DIAGNOSTICS_REQUEST`, `REBOOT`,
`REFRESH_CONFIG` (kept as an alias that forces a D1 re-read, so firmware
written against the old contract still works).

There is deliberately **no `IDENTIFY` action**: binding uses the QR claim code
(AD-18) and requires no device behaviour, because the hardware team has told us
we may not assume an LED, a buzzer, or a button.

The device answers on **D2** (not T4), so a single uplink lane carries every
device-originated event:

```json
{"schema_version": "1.0", "slot_id": "GW-2026-0042",
 "command_uuid": "b1f0…", "status": "ACKNOWLEDGED|SUCCESS|FAILED",
 "error": null, "ts": "2026-10-18T08:41:02Z"}
```

**Category B (actuators) keeps T3/T4** exactly as contracted, because it is
saloon-scoped and only ever runs after provisioning. Keeping the two categories
on separate topics is what lets Category A work on an unbound device (AD-19).

`add_dev` / `set_label` are **superseded** by D1. Firmware keeps parsing them
for backward compatibility (they are idempotent, per the contract), but the
backend never emits them after cutover. This resolves the 8-hex-vs-`P-A`
inconsistency in the corpus (`MQTT Topic Map` §1.3.1 says 8 hex digits;
`Firmware Spec` §3.2 shows `"pack_id": "P-A"`). The owner has confirmed
**8 hex digits** (§12.1 A8); the stale `"P-A"` example must be corrected in the
Firmware Spec during Phase 4.

### 6.3 Complete flow: power-on → activated

**Happy path.**

```
 0. FACTORY (once, before shipping)
    HW panel: POST /platform/devices/units/ {hardware_id, model, batch}
      → unit_id = "GW-2026-0042" (operator-supplied or generated),
        state = REGISTERED
      → credential row: username = unit_id, bcrypt($2b$) hash
      → ACL rows (3): subscribe devices/{unit_id}/desired
                      subscribe devices/{unit_id}/command
                      publish   devices/{unit_id}/reported
      → plaintext returned ONCE, written to encrypted flash by the line,
        and NEVER printed again. The installer will not need it. [AD-18]
    No org, no farm, no slot row exists yet. [solves G1]

 1. INSTALL REQUEST (org rep)  — the request may CREATE the farm [AD-15]
    POST /gateway-requests/
      { farm_id?                       -- existing farm, OR
        farm_spec {name, province_id, city_id, village_id?},
        expected_gateways: 2,          -- multi-gateway farm [AD-16]
        requirements, expected_equipment, contact, install_window }
      → status PENDING

 2. REVIEW + APPROVE (support)
    POST /gateway-requests/{id}/review/ {decision: APPROVE}
      → one transaction:
          · if farm_id is null → create Farm(org=request.org, from farm_spec)
          · create expected_gateways installation_jobs, all unassigned
          · request APPROVED  (no slot, no credentials — the device
            already exists at the factory) [AD-13 consequence]
      → audit rows: FARM_CREATED (when applicable), JOBS_CREATED

 3. ASSIGN (support, per job) [AD-11]
    GET  /provisioning/installations/?state=PENDING&province_id=…
    POST /provisioning/installations/{job_id}/assign/ {installer_user_id}
      → job.assigned_installer, assigned_by, assigned_at
      → job.state = ASSIGNED
      → installer's queue shows it (installers cannot decline — owner Q10)

 4. INSTALLER OPENS A JOB SESSION  [A14 / §5.6]
    POST /provisioning/installations/{job_id}/session/
      → gate: platform staff, assigned to this job
      → 15-min token: org_id + farm_id + job_id + installer_session
      → audit: INSTALLER_JOB_ENTER
    GET  /provisioning/installations/{job_id}/
      → farm (created in step 2), address, requirements, expected equipment

 5. BIND THE GATEWAY — scan the QR, no password, no device interaction [AD-18]
    Installer powers the unit, scans the label's QR
    (which encodes unit_id + claim_code) and takes a photo.
    POST /provisioning/installations/{job_id}/gateway/
         {unit_id, claim_code, photo?}
      → unit exists, state=REGISTERED, not bound to another job
      → bcrypt-verify claim_code against claim_code_hash   (401 on mismatch)
      → single transaction:
          · create GatewaySlot(slot_id=unit_id, org, farm,
                               PENDING_INSTALLATION)
          · unit.state = BOUND, unit.slot_id = slot_id,
            unit.claimed_at = now, claim_code_hash = NULL
          · ACLs: keep the 3 device rows, add T1..T9 for this org/farm
          · job.state = BOUND, job.gateway_slot = slot
          · audit: GATEWAY_BOUND
      → sync EMQX (postgres rows are truth; the push is FNV-soft)
    Works with the unit offline: binding is a paperwork step, activation is
    the device's job (step 9).

 6. HALLS + SENSORS (installer, repeatable) [AD-20/AD-7/AD-23]
    POST /farms/{farm}/saloons/                      [role widened]
    POST /saloons/{id}/positions/                    [role widened]
    POST /saloons/{id}/sensor-slots/                 [NEW — closes D5]
    POST /saloons/{id}/sensor-slots/{slot}/photo/    [NEW — device_photos]
    POST /provisioning/installations/{job_id}/sensors/
         {pack_id: "a1b2c3d4", sensor_slot_id, position_id}
         [NEW — writes GatewaySensorMapping]

 7. SUBMIT
    POST /provisioning/installations/{job_id}/submit/
      → desired_config_version += 1
      → gateway_commands row (kind=PROVISION, state=PENDING)
      → job.state = SUBMITTED
      → Celery publishes D1 retained, QoS 1
      → on PUBACK: command SENT, published_at set

 8. GATEWAY APPLIES
    receives retained D1 (immediately on subscribe if it was offline)
      → validates slot_id == self
      → applies org/farm/sensors, persists config_version to flash
      → publishes D2 {config_version: 7, state: APPLIED}

 9. BACKEND CONVERGES — no human approval [AD-17]
    subscriber consumes D2 → Redis stream → reconciler
      → applied_config_version = 7, applied_at = now
      → command state = APPLIED
      → if job.state == SUBMITTED and applied == desired:
            transition(slot, PROVISIONED, by=SYSTEM_ACTOR)
            job.state = COMPLETED
      → telemetry flows on T1
```

**On the removal of farm-manager approval (AD-17).** Two human gates remain: the
org rep requests, and support approves. The D18 gate existed to stop an
installer approving their own install; installers are now our own staff, so it
was oversight of employees rather than protection of the customer. What it did
protect against — a gateway going live with no working configuration — is now
enforced mechanically: `PROVISIONED` requires `applied == desired`. The
`PENDING_APPROVAL` state, the approve/reject endpoints, and the
`telemetry:pending_approval` buffer all stay in the codebase for legacy slots
but are no longer on the new path.

**System actor.** `provisioning_audit_log.changed_by` is a non-null FK
(`provisioning/models.py:21`), so an automatic transition needs an actor. Add a
`system` user via data migration (inactive, unusable password,
`is_platform_staff=False`) and a `get_system_user()` helper. Do not make the
column nullable — the audit trail's value is that every row names someone.

**Failure scenarios.**

| Scenario | Behaviour |
|---|---|
| **F1** Unknown username at CONNECT | Authn query returns no row → CONNACK failure. No oracle: the device sees a generic rejection. |
| **F2** Unit `RETIRED` | Authn query excludes it → cannot connect. Its ACL rows are deleted. |
| **F3** Device connects before binding | Allowed (state `REGISTERED`), but its ACL covers only D1/D2/D3. It cannot publish telemetry anywhere — there is no topic it is permitted to use. |
| **F4** Wrong or already-consumed claim code | 401 `invalid claim code`, identical for both cases so the endpoint is not a code oracle. Attempt audited and rate-limited per installer. |
| **F4b** Unit is offline at bind time | **Allowed.** Binding is a paperwork step; the config is retained and applies when the unit first reaches the broker. The job stays `BOUND` and appears in the "not yet activated" view (AD-22) until it reports. |
| **F5** Installer scans a unit already bound elsewhere | 409 `device already bound` — **and the response must not disclose the owning org or farm**. |
| **F5b** Installer scans a `RETIRED` unit | 404, identical to "no such unit", so the endpoint is not an inventory oracle. |
| **F6** Device offline when submit happens | Publish succeeds (broker accepts; session queues). Device receives retained D1 on reconnect. `applied < desired` until then. |
| **F7** Device offline and its EMQX session expired (24 h) | Retained D1 survives — retained messages are not session state. Device still converges. **This is the main reason retained was chosen over queued commands.** |
| **F8** Duplicate D1 delivery | `config_version == applied` → device re-publishes D2 `APPLIED`, performs no work. Idempotent. |
| **F9** Out-of-order D1 (v9 then v8) | Device ignores `config_version <= applied`. Monotonic guard. |
| **F10** Device applies then loses power before reporting | On boot, D1 retained is re-delivered; device sees `config_version == applied` and immediately re-reports `APPLIED`. Self-healing. |
| **F11** Publish fails (broker down) | Command stays `PENDING` with `last_error`; Celery retries with backoff; a beat sweep (`every 60 s`) republishes desired state for every slot where `applied < desired`. Converges when the broker returns. |
| **F12** Device reports `FAILED` | Command → `FAILED`, `error` stored, installer + support notified. `applied` unchanged. Retry is an explicit operator action, not automatic — an apply failure is usually a data problem. |
| **F13** Device sends D2 for a foreign slot_id | Reconciler rejects: the topic's slot segment must equal the payload's `slot_id` (same identity-fault rule as T1, `topics.py:38`). DLQ with `TOPIC_PAYLOAD_IDENTITY_MISMATCH`. |
| **F14** Rogue LoRa node broadcasts | Not in `sensors[]` → gateway drops it, increments `rejected_packs`, reports it in D2. Backend raises a diagnostic alert if it persists. |
| **F15** Two installers bind the same unit concurrently | `device_units.state` transition uses `SELECT … FOR UPDATE`; the loser gets 409. |
| **F16** Farm moved to another org (rare) | Not supported in v1. Documented as a `RETIRE` + re-`BIND` ceremony, because the topic namespace and RLS org both change. |

### 6.4 Answering Question One — fully MQTT, point by point

| Concern | Design |
|---|---|
| **Initial device authentication** | Username = factory-assigned `unit_id`, password from flash, verified by EMQX against the bcrypt hash in `mqtt_device_credentials`. No HTTP involved. |
| **Connection to the broker** | TLS 8883 only; plaintext 1883 is disabled (`emqx/emqx.conf:35-37`). MQTT 3.1.1 pinned fleet-wide (owner decision 2026-10-18). |
| **Gateway identification / validation** | Three independent checks: (a) broker authn (credential exists, active, unit not retired); (b) topic segment vs payload `slot_id` on every inbound message; (c) the retained D1's `slot_id` self-check on the device. |
| **Sending commands** | Two shapes: retained desired state (convergence) on D1, and one-shot Category A commands on D3 with `expires_at`. Category B actuators stay on T3. |
| **Receiving responses / confirming execution** | Everything the device says arrives on D2 (config apply, command ACK/result, heartbeat) → Redis stream → reconciler. T4 remains for actuator responses. |
| **Connection loss / reconnection** | Firmware already reconnects with backoff and buffers telemetry to SD (`Firmware Spec` §4). Retained D1 removes the need for the device to *ask* anything on reconnect: it re-subscribes and is immediately in sync. |
| **Preventing double processing** | `config_version` monotonic per slot; `command_uuid` ring buffer (last 32) persisted in flash for imperatives; the reconciler is idempotent (state transitions are compare-and-set). |
| **Device offline when a command is issued** | Config: retained, no expiry, converges on reconnect. Imperative: `expires_at` honoured by both sides — the backend marks `EXPIRED` at `expires_at + grace`, and the device discards an expired command rather than executing a stale "reboot" three days later. |
| **Security / topic-level access control** | Per-device literal ACL rows, `no_match=deny`. Before binding: 2 rules (D1 subscribe, D2 publish). After binding: + 9 T-topics. A device can never match another device's topic — there are no wildcards in any gateway ACL row. |

### 6.5 Answering Question Two — topic management

| | **(A) Firmware-derived from device id** | **(B) Backend sends literal topics** | **(C) Hybrid — chosen** |
|---|---|---|---|
| Determinism | High | Low — strings arrive at runtime | High — grammar is compiled in |
| Re-assignable (farm move) | **Impossible** — org/farm are server facts the device cannot derive | Yes | Yes — they are data, not code |
| Payload size | 0 | Large | 2 fields (`org_id`, `farm_id`) |
| On-device validation | None needed | Cannot validate an opaque string | Can verify the payload's `topics` block against its own derivation |
| Failure mode | Device computes a topic nobody listens on | Device silently uses a wrong-but-ACL-allowed topic | Device refuses a document that disagrees with its derivation |
| Security blast radius | n/a | A compromised publisher can aim a device at any topic its ACL allows | Same as B, but the device rejects mismatches, and ACLs remain literal |
| Firmware change on rebind | Reflash | None | None |
| Complexity | Lowest | Medium | Low — one template + two fields |

**Recommendation: (C).** Pure (A) is arithmetically impossible here because
the topic prefix contains `org_id` and `farm_id`, which are server-side
business facts and can change without touching the device. Pure (B) throws
away the only cheap integrity check available on a constrained device and
makes the topic set a runtime string blob that nobody can diff in review.

(C) is what the existing contract already does in spirit — the HTTP config
payload carries `org_id`/`farm_id` and the firmware composes
`{org}/farms/{farm}/gateways/{slot}/…` from them. We are keeping the data
contract and changing the transport. That is the definition of a low-cost
migration.

**Permissions before vs after provisioning:**

| Phase | Subscribe | Publish |
|---|---|---|
| `REGISTERED` (factory, unbound) | `devices/{id}/desired`, `devices/{id}/command` | `devices/{id}/reported` |
| `BOUND` / `PENDING_INSTALLATION` | + `…/commands`, `…/clock/sync`, `…/ota`, `…/diagnostics/request`, `…/status` | + `…/telemetry`, `…/status`, `…/commands/response`, `…/ota/response`, `…/diagnostics/response` |
| `PROVISIONED` | unchanged | unchanged |
| `SUSPENDED` / `REJECTED` | **all rows deleted** → `no_match=deny` | **all rows deleted** |

Note that the unbound state is *strictly less* privileged than today's
`PENDING_INSTALLATION` state, which already holds the full T1–T9 set for a
known org. The new model removes that pre-binding exposure.

---

## 7. Security design

### 7.1 Authentication changes

The authn query gains exactly one branch. Current
(`emqx/emqx.conf:102`):

```sql
... JOIN gateway_slots gs ON gs.slot_id = cred.device_id
WHERE cred.username = ${username} AND cred.is_active = true
  AND gs.provisioning_status IN ('PENDING_INSTALLATION','PENDING_APPROVAL','PROVISIONED')
```

Proposed:

```sql
SELECT cred.password_hash AS password_hash
FROM mqtt_device_credentials cred
LEFT JOIN gateway_slots gs ON gs.slot_id = cred.device_id
LEFT JOIN device_units   du ON du.unit_id  = cred.device_id
WHERE cred.username = ${username}
  AND cred.is_active = true
  AND (
        gs.provisioning_status IN
            ('PENDING_INSTALLATION','PENDING_APPROVAL','PROVISIONED')
     OR (gs.slot_id IS NULL AND du.state IN ('REGISTERED','BOUND'))
  )
```

Requirements this imposes:

- `GRANT SELECT ON device_units TO emqx_auth` (mirroring
  `initdb/01_roles.sql:62-68`).
- `device_units` must **not** be RLS'd (it has no `org_id`; the parity script
  only requires policies on org-bearing tables — verify with the existing
  parity check before merging).
- Both JOIN columns are unique/PK, so the cost is two index lookups per
  CONNECT.
- Validate with `docker exec morghdari-emqx emqx check config` before deploy
  (the project's standing rule for `emqx.conf` edits).

### 7.2 Credential lifecycle

| Event | Mechanism | State |
|---|---|---|
| Factory issue | Server-generated 32-byte `token_urlsafe`, bcrypt `$2b$`, plaintext returned **once** | **[IMPL]** pattern from `api.py:523-537` |
| Rotation | Staged: `pending_password_hash`; old password stays valid at the broker; commit only when the device proves receipt | **[IMPL]** `services.py:365-405` |
| Rotation trigger over MQTT | `T3 {action: ROTATE_CREDENTIAL}` → device reconnects → `T4 SUCCESS` → reconciler calls `commit_pending_rotation` | **[MISSING]** — closes D9 |
| Retirement | `is_active=false` + ACL rows deleted + `unit.state=RETIRED` | **[MISSING]** |
| Expiry | `expires_at` column exists but nothing enforces it | **[MISSING]** — add a beat sweep |

**Rotation without plaintext on the wire is not possible here** and should not
be attempted: EMQX's built-in user API only accepts plaintext, and the device
needs the literal new password to reconnect. The mitigation is transport
(TLS 8883) plus the staged-commit guarantee (a lost delivery can never lock a
device out). Do not log `params.new_password` — add an explicit redaction test.

### 7.3 Threat model

| # | Threat | Control | Residual risk |
|---|---|---|---|
| S1 | Credential theft from device flash | TLS-only transport; per-device credential; rotation; `is_active` kill switch | Physical extraction from flash — mitigate with flash encryption (firmware) |
| S2 | Device impersonates another device | Username is the identity; ACL rows are literal per-username; D1 self-check; T1 identity-fault gate | None beyond credential compromise |
| S3 | Cross-tenant topic access | No wildcards in any gateway ACL row; `no_match=deny`; org prefix in T-topics | Accepted: a `django-backend` publisher credential can publish to any org's `T3` (documented accepted risk, `Topic Map` §3.3). Mitigate by making the publisher's ACL rows the real bound and keeping the credential in the vault. |
| S4 | Unauthorized device claiming | Three gates: an authenticated installer **assigned to that job**, a valid **single-use claim code** from the physical label, and a unit in `REGISTERED` state. The claim code is bcrypt-hashed at rest and destroyed on use. | Someone who photographs the label before installation can claim the unit first. That is visible (the audit row names who claimed it) and reversible (support re-issues a claim code), and it is a denial-of-service, not a breach — the impostor still cannot make the unit authenticate. |
| S4c | MQTT password leaking through a field technician | The installer never sees it. It exists only in the device's flash and as a bcrypt hash server-side; the claim code is a separate, single-use secret that cannot be used for broker auth. | None. This is strictly better than the original "installer types the password" design. |
| S4b | Installer over-reach: reading a customer org beyond their job | `installer_session` is per-job, 15-minute, RLS-scoped to one org, and every installer endpoint filters by the claim's `farm_id` (§5.6). It cannot open `_support_op_context`. | A malicious installer can read the assigned farm's data while the session lives. Accepted, audited (`INSTALLER_JOB_ENTER`). |
| S5 | Sensor bound to two gateways | `UNIQUE(hardware_id) WHERE removed_at IS NULL` on the sensor registry (closes D6) + `UNIQUE(gateway_hardware_id, pack_id)` already present (`devices/models.py:208`) | A duplicated physical node with a cloned LoRa id — out of scope (radio-layer) |
| S6 | Replay of a provisioning command | `config_version` monotonic; `command_uuid` dedupe ring; `expires_at` on imperatives | Retained D1 replays are *intended* behaviour and harmless (idempotent) |
| S7 | Malicious retained message | Only the `django-backend` principal can publish to `devices/+/desired` (ACL) | Compromised publisher credential — same class as S3 |
| S8 | Password in logs | Never serialize `params.new_password`; structured logging with an allow-list of fields | Human error in future code — add a test that greps the log capture for the plaintext |
| S9 | Enumeration of valid slot ids | Bind endpoint returns an identical 404/409 shape regardless of whether the unit exists in another org | Timing side channel — negligible |
| S10 | Stale command executed days later | `expires_at` enforced on both sides | Clock skew on the device — bounded by the existing clock-drift flag |

### 7.4 Where your proposed logic needs correction

Being explicit, as you asked:

1. **"Store the password using Django's Password Hasher"** — this would break
   broker authentication. Device credentials must stay on EMQX-native bcrypt.
   The existing code is already correct; do not "fix" it.
2. **"Sensors broadcast their identity, the gateway must distinguish
   authorized sensors"** — correct, but the enforcement point must be *both*
   ends. Gateway-side filtering alone is bypassable by a compromised gateway
   that simply reports readings for a pack it was never given.
3. **"Send the information required … through MQTT (there may already be
   `add_dev`)"** — `add_dev` is documented but unimplemented, and as specified
   it has no correlation id, no acknowledgement, and last-writer-wins
   replacement semantics. Building it as-is would give you a command channel
   that cannot tell you whether it worked — the exact failure mode your §7
   requirements forbid.
4. **"The installer enters the device username and password"** — good, but it
   must be a *bind* operation that creates the org/farm linkage atomically
   with the credential check. Validating the credential and then binding in a
   second call opens a window where two installers can both pass validation.
5. **Implicit assumption that the installer is an org member** — the
   requested flow routes by province across orgs. The current
   `AccountRoleScope.province` (`accounts/models.py:89`) scopes an installer
   *within one org*. **Resolved 2026-10-18:** the owner confirmed installers are
   company staff, so they reach tenant data through a job-scoped session rather
   than an `OrgMember` row (A4 / AD-14 / §5.6).

---

## 8. Installation workflow (end to end)

| # | Actor | Action | System effect | Status |
|---|---|---|---|---|
| 1 | Hardware (company staff) | Register unit, issue credential, write to flash | `device_units` + credential + 3 ACL rows | **[MISSING]** |
| 2 | Org rep | Submit request: farm spec **or** existing farm, requirements, expected equipment, expected gateway count | `GatewayRequest` PENDING | **[IMPL]** — currently requires an existing `farm_id` and has no gateway count |
| 3 | Support | Approve: materialise the farm if new, create N jobs | `Farm` (new), N `installation_jobs`, request APPROVED | **[PARTIAL]** — approval exists, farm/job creation does not |
| 4 | Support | Assign a specific installer to each job | `job.assigned_installer` set | **[MISSING]** |
| 5 | Installer | Open a job session, see farm + requirements | 15-min scoped token; job detail | **[MISSING]** |
| 6 | Installer | Scan the unit's QR (`unit_id` + `claim_code`), take a photo | Unit BOUND, slot created, ACLs expanded | **[MISSING]** |
| 7 | Installer | Create halls | `Saloon` rows | **[IMPL]** — manager-only today, needs widening |
| 8 | Installer | Add sensor slots, map pack ids, upload photos | `SaloonSensorSlot`, `GatewaySensorMapping`, `device_photos` | **[MISSING]** |
| 9 | Installer | Submit | `desired_config_version++`, publish D1, job SUBMITTED | **[MISSING]** |
| 10 | Gateway | Apply D1, report D2 | `applied_config_version` updated | **[MISSING]** |
| 11 | — (system) | Convergence check | `PROVISIONED` when applied == desired **and** job submitted | **[MISSING]** — replaces the removed FM approval |
| 12 | Support / ops | Watch the "not yet configured" view | `GET /provisioning/config-state/?state=pending|failed` | **[MISSING]** — A22 |

Steps 1, 3(partial), 4–6, 8–12 are the build. Steps 2 and 7 are reuse plus a
schema/role change.

---

## 9. Migration plan

Sequenced so that **every phase is independently deployable, reversible, and
leaves the old path working.** No phase requires a coordinated cutover.

### Phase 0 — Foundations (no behaviour change)

| Task | Content | Reversible by |
|---|---|---|
| 0.1 | `device_units` model + migration (new table, no RLS) | drop table |
| 0.2 | `gateway_commands` model + migration (org-scoped, RLS-enrolled) | drop table |
| 0.3 | Additive columns on `gateway_slots`: `desired_config_version`, `applied_config_version`, `applied_at`, `last_publish_error` (all defaulted) | drop columns |
| 0.4 | `device_units` SELECT grant to `emqx_auth` in `initdb/01_roles.sql` + a migration twin | revoke |
| 0.5 | `django-backend` publisher principal seeded via a management command (mirroring `seed_subscriber_credentials.py`) | delete rows |
| 0.6 | `apps/provisioning/publisher.py` — paho client, TLS, QoS 1, bounded `wait_for_publish`, injectable for tests. **This is v0.5 task E1** (`apps/mqtt_auth/publisher.py`); keep the specified location if there is no objection, so the old task closes cleanly | delete module |
| 0.7 | Celery tasks: `publish_desired_state`, `publish_command`, `reconcile_reported` | remove from beat |
| 0.8 | `installation_jobs` model + migration (org-scoped, RLS-enrolled) — A16 | drop table |
| 0.9 | `device_photos` model + migration (org-scoped, RLS-enrolled, exactly-one-target CHECK) — A20 | drop table |
| 0.10 | `gateway_requests`: `farm_id` → nullable, add `farm_spec` JSON, `expected_gateways` int default 1 — A15/A16 | drop columns |
| 0.11 | System user via data migration + `get_system_user()` helper (for automatic `PROVISIONED`) | reverse migration |
| 0.12 | `installer_session` claim: shared token issuer + middleware branch ①b (§5.6) | remove branch |

**Exit criteria:** nothing behaves differently. Full suite green. A
management command can publish a retained message to a topic and the
subscriber's metrics counter moves.

> **STATUS 2026-10-18: IMPLEMENTED.** All twelve tasks are in the tree and green
> (`backend/tests/test_m10_00_foundations.py`, 27 tests). Two deviations from
> the plan: the RLS for the new tables went into a **new**
> `tenants/0006_rls_phase0_tables.py` rather than into the frozen
> `0002_rls_policies.py`; and `gateway_requests.farm_id` needed no migration
> because the column was already nullable (only the serializer requires it —
> that is a Phase 2 change). The live-broker half of the exit criterion moves
> to Phase 1, since it needs the authn change and a running EMQX.

### Phase 1 — Hardware inventory (unblocks G1)

| Task | Content |
|---|---|
| 1.1 | `POST /platform/devices/units/` — register unit (platform-staff, org-less gate) |
| 1.2 | `POST /platform/devices/units/{id}/credential/` — issue MQTT password (plaintext once, to the factory line only) **and** the single-use `claim_code` for the QR |
| 1.3 | `POST /platform/devices/units/{id}/retire/` |
| 1.4 | `POST /platform/devices/units/{id}/claim-code/` — re-issue a lost label's code (rotates the hash; audited) |
| 1.5 | `GET /platform/devices/units/` — inventory list with state filter |
| 1.6 | New ACL template `bootstrap_acl_rule_items()` — 3 rows (subscribe `desired`, subscribe `command`, publish `reported`) |
| 1.7 | Authn query branch + `emqx.conf` update + `emqx check config` |

**Exit criteria:** a unit can be registered with no org, connect to the broker
over TLS with its factory credential, and subscribe to its own `desired`
topic. It cannot publish to any tenant topic (assert with a denied-publish
test against a live broker, or by asserting the ACL row set).

> **STATUS 2026-10-18: IMPLEMENTED.** `apps/devices/api_units.py` (register /
> credential / claim-code / retire / list), `apps/accounts/platform_gate.py`,
> `PlatformAuditLog`, `bootstrap_acl_rule_items()`, and the authn-query change
> in `emqx/emqx.conf`. `tests/test_m10_01_device_units.py` = 27 tests green,
> including the assertion that an unbound unit's ACL is three literal rows with
> no wildcard and no tenant prefix.
>
> Two pre-existing defects were fixed in the same pass, with your sign-off that
> this is not production code: the **dead status lane** (D1/D2 — four defects:
> never routed, wrong table, wrong case, wrong client) and the **`farms/None`
> ACL** (D3). Regression tests: `tests/test_m10_02_status_lane.py`, 15 green.

**Risk:** this is the only phase touching `emqx.conf`. Mitigation: config
validation + a rehearsal-stack smoke test before touching production; the old
query is preserved as a comment for instant rollback.

### Phase 2 — Request → approve → assign → bind → submit → converge

| Task | Content |
|---|---|
> **SLICE (a) STATUS 2026-10-19: DONE.** `GatewayRequestCreateSerializer` validates
> exactly-one-of `farm_id` / `farm_spec` (+ `expected_gateways`), the POST view now
> runs the body through it (it used to read `request.data` raw), and approval
> materialises the Farm and bulk-creates one `InstallationJob` per gateway.
> Approval no longer mints slot/credential/ACLs, so its response carries no
> password. Two `Farm` field mistakes were caught by the new tests (it has
> neither `notes` nor `created_by`). Only the two pre-existing baseline
> failures remain in that module.

| 2.1 | Request create: accept `farm_spec` **or** `farm_id`, plus `expected_gateways` (AD-15/AD-16) |
| 2.2 | Approval: materialise the `Farm` from `farm_spec`, create N `installation_jobs`, **stop minting slot/credential/ACLs** (see the warning below) |
| 2.3 | `POST /provisioning/installations/{job_id}/session/` — installer job session (§5.6) |
| 2.4 | `GET /provisioning/installations/` — installer queue; `GET …/{job_id}/` — job detail |
> **SLICE (b) STATUS 2026-10-19: DONE.** `apps/provisioning/installations.py`
> implements the queue, the job detail, the assignable picker and the scoped job
> session under `/provisioning/installations/`. The token carries
> `installer_session`, never `support_session` (AD-14).
> Three bugs were found by the tests while building it: an installer was shown
> the WHOLE org queue (fixed — an installer sees only their own), an unassigned
> job returned 403 where 409 is the honest verdict, and support opening a
> session on a dead installer's behalf was blocked for no good reason (allowed
> and audited).

| 2.3 | `POST /provisioning/installations/{job_id}/session/` — installer job session (§5.6) |
| 2.4 | `GET /provisioning/installations/` — installer queue; `GET …/{job_id}/` — job detail |
| 2.5 | `POST /provisioning/installations/{job_id}/assign/` — support assigns; picker filtered by province |
| 2.6 | `POST …/{job_id}/gateway/` — atomic bind by `unit_id` + `claim_code` (create slot, expand ACL, consume code, audit); works with the unit offline |
> **SLICE (c) STATUS 2026-10-19: DONE.** `_bind_job_to_unit()` +
> `install_job_bind` / `install_job_bind_override`. The ACL is EXPANDED, not
> replaced — 3 device-lane rows survive and 9 tenant rows are added (a test
> asserts exactly 12, because a fixture that omits the device lane would let a
> silent replacement pass). The retained publish happens in
> `transaction.on_commit`, so a broker outage degrades to "not yet published"
> and the beat sweep converges it. `GatewayCommand` has no `action` column —
> `kind` is the discriminator.

| 2.6 | `POST …/{job_id}/gateway/` — atomic bind by `unit_id` + `claim_code` (create slot, expand ACL, consume code, audit); works with the unit offline |
| 2.7 | `POST …/{job_id}/gateway/override/` — support-only bind without a claim code (lost label), mandatory reason, audited |
> **TASK 2.8 STATUS 2026-10-20: DONE.** Contradiction X3 is resolved. The
> endpoint lives in `apps/farms/api.py` (not provisioning) because a sensor slot
> is a farms/saloon resource that an installer session writes by carrying an org
> claim — the same shape as the flock writer next to it.

| 2.8 | `POST /saloons/{id}/sensor-slots/` — the missing writer (D5) |
> **TASK 2.9 STATUS 2026-10-20: DONE.** Implemented as two paths under the
> job (gateway + sensor) rather than one under a bare saloon, because a
> `DevicePhoto` needs an org-scoped target the caller is authorised for, and the
> job is what proves that authorisation. Reuses `ops_services.photo_error` so
> the §2 O6 limits are shared with the existing saloon-photo endpoint.

| 2.9 | `POST /saloons/{id}/sensor-slots/{slot}/photo/` — `device_photos` upload (AD-20) |
> **TASK 2.10 STATUS 2026-10-20: DONE.** With 2.8 above this closes the
> sensor story: 2.8 creates the mounting plan, 2.10 binds the physical sensor to
> it, and `build_desired_state` already reads the join — so the device now
> receives `sensors[].pack_id` with its saloon, position, label and metrics.

| 2.10 | `POST …/{job_id}/sensors/` — pack → sensor-slot mapping |
| 2.11 | `POST …/{job_id}/submit/` — bump desired version, enqueue publish, job SUBMITTED |
| 2.12 | `build_desired_state(slot)` — pure function, reuses `gateway_config_payload` field names |
| 2.13 | Subscriber lane `$share/telemetry_workers/devices/+/reported` → Redis stream |
| 2.14 | Reconciler: reported → `applied_config_version`; auto-`PROVISIONED` when applied == desired and job submitted (system actor) |
| 2.15 | Beat sweep: republish where `applied < desired` (60 s) |
> **TASK 2.16 STATUS 2026-10-20: DONE.** Both writers accept an installer
> session scoped to the farm being installed; `support_session` does not, and a
> session without a `farm_id` claim is refused rather than widened.

| 2.16 | Role widening for `saloon_create` / `position_create` (installer session + `farm_id` filter) |
> **SLICE (d)+(e) STATUS 2026-10-19: DONE.** Tasks 2.11 and 2.17 shipped —
> `install_job_submit` and `provisioning_config_state`. 2.12/2.13/2.14/2.15
> already existed from Phase 0, so the only missing piece of the no-approval
> path was the submit endpoint. `test_submit_activates_without_human_approval`
> drives the REAL reconciler and asserts a slot goes PROVISIONED with no manager
> involved. Note PENDING_SYNC is unreachable for a slot created by the bind (it
> always bumps desired to 1); it exists for slots created by any path that
> publishes nothing.

| 2.11 | `POST …/{job_id}/submit/` — bump desired version, enqueue publish, job SUBMITTED |

**Review-approval change (do not miss this).** Today
`review_gateway_request` mints the slot, the credential, and the ACL rows in
the same transaction (`provisioning/api.py:519-552`). Under the new flow the
device already exists at the factory, so approval must **stop** minting them —
otherwise every approved request creates an orphan slot with a credential no
device holds. Move slot creation to the bind step (2.7) and leave approval as
farm materialisation + job creation + status change + audit row. This is the
single most delicate edit in the whole plan; existing approved requests must
keep working (see the compatibility promise in Phase 4).

**Exit criteria:** on the simulator or a HIL unit, a full
request→approve→assign→session→identify→bind→configure→submit→converge cycle
completes, `applied == desired`, the slot reaches `PROVISIONED` with no human
approval, and the gateway publishes telemetry on T1. Duplicate submit is a
no-op. Killing the gateway mid-flow and restarting converges without operator
action.

### Phase 3 — Command channel (closes D9)

| Task | Content |
|---|---|
| 3.1 | Subscriber lane `+/farms/+/gateways/+/commands/response` |
| 3.2 | `ROTATE_CREDENTIAL` end-to-end (publish → ACK → reconnect → SUCCESS → commit) |
| 3.3 | `DIAGNOSTICS_REQUEST` end-to-end |
| 3.4 | Expiry sweep (`expires_at` → EXPIRED) + attempt counter |
> **PHASE 3 STATUS 2026-10-20: 3.1–3.4 DONE; 3.5 was already fixed.**
> `reconcile_command_response` implements the Blueprint §11.2 matrix
> (SENT → ACKNOWLEDGED → APPLIED/FAILED) and the subscriber now subscribes and
> routes `+/farms/+/gateways/+/commands/response` — the lane that made the whole
> channel dead. `sweep_command_expiry` runs every 60 s so a SENT command with no
> answer becomes EXPIRED instead of sitting in flight forever.
> Two rules worth naming: a late response NEVER resurrects a FAILED/EXPIRED row
> (it records `late …` in `last_error`), and an unknown command_uuid is IGNORED
> rather than treated as a fault, because devices replay their outbox.
> `ROTATE_CREDENTIAL` commits only when the device PRESENTS the new password —
> a bare SUCCESS is not proof it can authenticate with it.
> Tests: `test_m13_00_command_channel.py` (18).
> Note: `ck_gateway_commands_kind` already rejects unknown kinds at the storage
> layer, so the reconciler's no-op branch is a second line of defence, not the
> first.

| 3.5 | Status-lane fix (D1/D2) so LWT is honest |

> **Scope note (§1.4).** Tasks 3.1–3.4 are not new design work: they are v0.5
> tasks **E1–E6** plus the Blueprint §11.2 state matrix, which were fully
> specified and never implemented. Implement them as written, then extend the
> envelope with `PROVISION` so config pushes and imperative commands share one
> journal. Task 3.5 is genuinely new (the status lane was never correctly
> wired).

### Phase 4 — Cutover and retirement

| Task | Content |
|---|---|
| 4.1 | `system_configuration.mqtt_config_enabled` default flips to true |
| 4.2 | HTTP config endpoint: keep serving, add `Deprecation` + `Sunset` headers |
| 4.3 | Remove `add_dev` emission (never emitted) and mark it deprecated in the docs |
| 4.4 | Update `MQTT Topic Map`, `Firmware Spec`, `design-zero-touch-provisioning.md` to the new flow |
| 4.5 | After one firmware generation: retire the HTTP endpoint |

**Compatibility promise:** no existing device breaks. A
`PENDING_INSTALLATION` slot that was approved under the old flow keeps its
T1–T9 ACLs and its HTTP config endpoint. The only behavioural change for it is
that the backend *also* publishes D1 — which old firmware will ignore (it does
not subscribe to that topic, and its ACL would need the row, which we only add
at bind time under the new flow).

---

## 10. Impact analysis

### 10.1 New files

| Path | Purpose |
|---|---|
| `backend/apps/devices/migrations/000X_device_units.py` | new table |
| `backend/apps/devices/migrations/000X_gateway_config_version.py` | additive columns |
| `backend/apps/devices/migrations/000X_device_photos.py` | per-device photos (AD-20) |
| `backend/apps/provisioning/migrations/000X_gateway_commands.py` | new table + RLS enrolment |
| `backend/apps/provisioning/migrations/000X_installation_jobs.py` | new table + RLS enrolment (AD-16) |
| `backend/apps/provisioning/migrations/000X_request_farm_spec.py` | `farm_id` nullable, `farm_spec`, `expected_gateways` (AD-15) |
| `backend/apps/accounts/migrations/000X_system_user.py` | data migration for the system actor |
| `backend/apps/provisioning/publisher.py` | paho publish client |
| `backend/apps/provisioning/desired_state.py` | payload builder (pure function, unit-testable) |
| `backend/apps/provisioning/reconciler.py` | reported-state → DB, auto-`PROVISIONED` |
| `backend/apps/provisioning/installation_api.py` | session / queue / assign / identify / bind / sensors / submit |
| `backend/apps/provisioning/jobs.py` | farm materialisation + job creation on approval |
| `backend/apps/devices/api_units.py` | hardware inventory endpoints |
| `backend/apps/devices/sensor_slot_api.py` | sensor-slot writer (D5) |
| `backend/apps/accounts/session_tokens.py` | shared issuer for support + installer sessions |
| `backend/apps/mqtt_auth/management/commands/seed_publisher_credentials.py` | broker principal |
| `backend/tests/test_m10_*.py` | see §11 |

### 10.2 Modified files

| Path | Change | Risk |
|---|---|---|
| `backend/apps/devices/models.py` | +`DeviceUnit`, +`DevicePhoto`, +4 columns on `GatewaySlot` | Low (additive) |
| `backend/apps/provisioning/models.py` | +`GatewayCommand`, +`InstallationJob` | Low |
| `backend/apps/provisioning/api.py` | `gateway_acl_rule_items` must tolerate `farm_id=None` (D3); **review approval stops minting slot/credential/ACLs**; installer-role check accepts `installer_session` | **High** — the approval transaction and the RBAC gate |
| `backend/apps/provisioning/services.py` | `_restore_db_acl_rules` must regenerate on farm change (D4) | Medium |
| `backend/apps/tenants/middleware.py` | new branch ①b for `installer_session` (§5.6) | **High** — every request passes through it |
| `backend/apps/accounts/views.py` | `support_session` refactored onto the shared issuer; **`_support_op_context` must keep rejecting `installer_session`** | Medium |
| `backend/apps/accounts/api_hardware.py` | delegate to `device_units` for factory registration; org-less platform-staff gate | Medium |
| `backend/apps/farms/api_follow.py` | widen `saloon_create`/`position_create` to installer session + `farm_id` filter | Medium — RBAC surface |
| `backend/morghdari/subscriber/main.py` | +2 lanes (`devices/+/reported`, `commands/response`); fix `_on_message` status routing (D1/D2) | **Medium** — the ingestion hot path |
| `backend/morghdari/subscriber/topics.py` | +`parse_device_topic` for `devices/{id}/reported` | Low |
| `emqx/emqx.conf` | authn query branch | **High** — broker-wide |
| `initdb/01_roles.sql`, `initdb/02_rls.sql` | grant + RLS list (3 new tables) | High — must mirror the migrations |
| `backend/config/settings/base.py` | beat entries for sweep + expiry | Low |
| `docker-compose.prod.yml` | publisher env on the celery worker | Low |

### 10.3 Untouched (deliberately)

`apps/telemetry/` pipeline and drains, `apps/alerts/`, `apps/monitoring/`,
`apps/reports/`, `apps/compat/`, the `command_logs` table, and every existing
migration. The telemetry path must not be perturbed by this work.

### 10.4 New `system_configuration` keys

| Key | Default | Purpose |
|---|---|---|
| `mqtt_config_enabled` | `0` → `1` at cutover | Feature flag for the whole path |
| `mqtt_publish_max_attempts` | `5` | Publisher retry cap |
| `config_apply_timeout_sec` | `300` | desired→applied grace before alerting |
| `command_expiry_grace_sec` | `60` | grace past `expires_at` before EXPIRED |
| `desired_state_sweep_interval_sec` | `60` | convergence sweep cadence |

---

## 11. Test strategy

Conventions: `backend/tests/test_*.py`, pytest with `--nomigrations`;
`@pytest.mark.use_migrations` only for RLS assertions (the project's stated
exception, `backend/pytest.ini:16-18`). Coverage gate is 80%.

### 11.1 Per-phase acceptance

| Phase | Test file | Scenarios |
|---|---|---|
| 0 | `test_m10_00_foundations.py` | migration applies cleanly; `GatewayCommand` state CHECK rejects bad values; publisher module is import-safe without a broker; publish failure returns False (never raises) |
| 1 | `test_m10_01_device_units.py` | register without org; duplicate `hardware_id` → 409; credential issued once (second call returns no plaintext); claim code issued once and stored only as a hash; re-issue rotates the hash; retire blocks connect; `bootstrap_acl_rule_items` emits exactly 3 rows; non-staff gets 403; **no** tenant topic appears in a `REGISTERED` unit's ACL |
| 1 | `test_m10_01_authn_query.py` (`use_migrations`) | the SQL returns a row for `REGISTERED`/`BOUND` and none for `RETIRED`/`SUSPENDED`/`REJECTED`; run as `emqx_auth` to prove the RLS exemption still works |
| 2 | `test_m10_02_bind.py` | happy path by QR claim code; wrong **and** already-consumed code both 401 with an identical body (no oracle); claim code is destroyed on success; already-bound 409 with no org leak; retired unit 404; concurrent bind (two threads) → exactly one winner; bind succeeds with the unit offline; ACL expansion is exactly T1–T9 + D1/D2/D3; farm-less slot never produces `farms/None` (D3 regression); support override requires a reason and writes an audit row |
| 2 | `test_m10_02b_config_state.py` | the four derived states, including a unit that never reported → `STALLED`; publish failure → `FAILED`; no state is ever stored, only computed |
| 2 | `test_m10_03_desired_state.py` | payload contains `org_id`, `farm_id`, complete `sensors[]`, `metrics` from `allowed_metric_keys`; **no plaintext password, no bcrypt hash, no `slot_id` leakage beyond self**; version is monotonic; two submits bump by exactly 1 |
| 2 | `test_m10_04_reconciler.py` | `applied` recorded; stale version ignored; foreign `slot_id` in payload → DLQ; duplicate report is idempotent; `applied == desired` clears PENDING |
| 2 | `test_m10_05_offline_convergence.py` | publish while "offline" (no subscriber), then deliver retained → converges; session expiry simulated → still converges |
| 3 | `test_m10_06_commands.py` | rotate end-to-end; ACK→SUCCESS ordering; late SUCCESS after timeout → stays FAILED + audit row; duplicate `command_uuid` executed once; expired command not applied |
| 3 | `test_m10_07_status_lane.py` | status message updates the LWT key (D1/D2 regression); malformed status still DLQs |
| 4 | `test_m10_08_cutover.py` | flag off → old behaviour byte-identical; flag on → new path; legacy slot still works |

### 11.2 End-to-end

Extend `simulator/sim.py` with a `--provision` mode that:
1. connects with a factory credential,
2. subscribes to `devices/{id}/desired`,
3. asserts the applied `config_version`,
4. publishes `reported`,
5. then publishes telemetry on T1 and asserts it lands in
   `SaloonTelemetry` via the normal drain.

That single harness proves the whole chain: broker ACL → publisher →
gateway apply → reported → reconciler → telemetry.

### 11.3 Security tests (mandatory, not optional)

- A `REGISTERED` unit publishing to `{org}/farms/1/gateways/OTHER/telemetry`
  is denied by the broker.
- A bound unit publishing to a *different* org's prefix is denied.
- A suspended slot's every publish is denied within the ACL cache TTL.
- No test log capture ever contains a plaintext password **or a claim code**
  (grep the captured records).
- `django-backend` can publish to `devices/+/desired` and **cannot**
  subscribe to telemetry.
- **A `support_session` token cannot open an `installer_session`, and vice
  versa** — both directions, asserted on the claim, not on the user.
- **An installer session cannot reach `_support_op_context`** (suspend/resume,
  alert force-resolve) — the 403 must be the same one an ordinary tenant gets.
- **An installer session cannot read another farm in the same org**: call every
  installer endpoint with a `farm_id` from the same org but a different job,
  and assert zero rows. This is the test that catches a missing `farm_id`
  filter (R13).

---

## 12. Questions and answers

### 12.1 Answered (owner, 2026-10-18)

| # | Question | Answer | Landed as |
|---|---|---|---|
| A1 | Who is the Hardware Team? | Employees of the software company | AD-13 — org-less `device_units`, platform-staff gate |
| A2 | How is work routed to an installer? | Support assigns a specific installer by hand | AD-11 — no routing engine |
| A3 | Does the request create the farm? | Yes | AD-15 — `farm_spec`, farm materialised at approval |
| A4 | Is an installer your employee or the customer's? | A member of our organization | AD-14 — platform staff + job-scoped `installer_session` (§5.6) |
| A5 | One request = one gateway or several? | Several — a request is for one farm, prepare for many gateways | AD-16 — `installation_jobs`, one per gateway |
| A6 | Does farm-manager approval stay? | Remove it — unnecessary complexity | AD-17 — replaced by the technical gate `applied == desired` |
| A7 | May the installer create halls? | Yes | AD-8 amended — installers may create saloons/positions within their assigned farm |
| A8 | LoRa sensor id format | 8 hexadecimal digits (4 bytes) | `pack_id` = 8 lowercase hex chars; correct the stale `"P-A"` example in the Firmware Spec |
| A9 | `username == slot_id`? | Yes | unchanged |
| A10 | Photos | One per sensor as well — each sensor is an independent entity | AD-20 — `device_photos` |
| A11 | May an installer decline a job? | No | no accept/decline state; assignment is binding |
| A12 | Should the node id be printed on the sensor, and entered when assigning it to a hall? | Yes — it is the identity, and the installer reads it rather than inventing it | A23 — `pack_id` mandatory on every mapping, printed on the sensor label |
| A13 | What does the installer scan? | The username, printed on the device or in the QR | AD-18 — QR carries `unit_id` + a single-use `claim_code` |
| A14 | May a job be reassigned after bind? | Yes — and the same machinery covers repair and maintenance | AD-21 — `installation_jobs.kind`, reassignment at any state |
| A15 | Should the frontend show which devices missed their settings? | Yes, explicitly | A22 — the four-state convergence view (§5.3) |
| A16 | May we assume the device has an LED? | No — "how the device works isn't important, what matters is that it can behave the way we want" | AD-18 rewritten: binding needs **no** device behaviour at all |

### 12.2 Resolved

Both clarifications from the previous round are closed by the answers above:

- **Reassignment after bind** → allowed (AD-14 / AD-21). Record
  `reassigned_after_bind` in the audit row and keep the original installer in
  the job history.
- **Binding a unit whose label is lost or damaged** → support-only override
  (`POST …/{job_id}/gateway/override/`) with a mandatory reason, always
  audited. Re-issuing a claim code (`POST /platform/devices/units/{id}/claim-code/`)
  is the normal path when the hardware team still has the unit.

### 12.3 Answering the node-id question directly

**Yes — the node id should be printed on the sensor, and the installer should
enter it when assigning the sensor to a hall.** Three reasons, in order of how
badly it breaks without it:

1. **Without it nothing resolves.** The gateway filters LoRa traffic by node
   id and reports `pack_id` in telemetry; the backend resolves
   `pack_id → slot` through `GatewaySensorMapping`. No mapping means every
   reading dead-letters as `UNKNOWN_SLOT` (`morghdari/subscriber/resolver.py`).
   It is not metadata — it is the join key.
2. **It cannot be derived.** The node id is burned at manufacture. The
   installer must read it from the label; if they type it, a typo silently
   produces a sensor that never reports.
3. **"By default" means at manufacture, not at installation.** The node id is
   assigned by the hardware team when the sensor is built and printed on the
   casing/QR. The installer's job is to *record* it, not to choose it.

**One thing to fix while we are here.** There are currently two identifiers for
the same physical sensor: `pack_id` (the 8-hex node id, used by the gateway)
and `HardwareRegistry.hardware_id` (a vendor serial, used for install
history). Maintaining both invites them to disagree. Recommendation: the
installer enters **only the node id**; `hardware_id` is optional and
informational. If the vendor serial and the node id are the same string, say so
and drop the second field from the UI.

### 12.4 Two things your answers changed that you may not have intended

**1. Removing farm-manager approval removes a security control, not just a step.**
The D18 gate (`Actor Use-Case & Dependency Map.md:142`, implemented at
`provisioning/api.py:793`) was added specifically because "installers must NOT
be able to pre-approve their own installs". Now that installers are your own
staff, that reasoning no longer applies — but two consequences follow:

- The `PENDING_APPROVAL` state and the `telemetry:pending_approval` buffer stop
  being used for new devices. They stay in the code for legacy slots. Do not
  delete them; a future tenant may want the gate back.
- The only remaining protection against "gateway live with no working config"
  is the `applied == desired` condition. It must be enforced in the reconciler,
  not in the UI, or a client bug will activate unconfigured devices.

**2. Installers as platform staff means the support-session mechanism is now
load-bearing for field operations.** If the 15-minute token expires mid-install
(a slow site visit, a phone that sleeps), the installer must silently re-open
it. Test that path explicitly — a field engineer blocked at a farm gate with an
expired token is a support call, not a bug report.

### 12.5 Definitions you asked about

**"Reassignment" (from Q10).** Support assigns job #12 to Ahmad. Two weeks
later Ahmad is unavailable, so support changes the assignee to Reza. That is a
reassignment. It is *not* the installer rejecting work — you said installers
cannot decline, so there is no accept/decline step at all; a job appears in
their queue the moment support assigns it.

Because you also want the same machinery for repair and maintenance, a job
carries a `kind`:

| Kind | Created by | Ends when |
|---|---|---|
| `INSTALL` | approval of a gateway request | config applied + submitted (→ `PROVISIONED`) |
| `REPAIR` | support, against an existing slot | the technician marks it resolved |
| `MAINTENANCE` | support, against an existing slot | the technician marks it resolved |

Only `INSTALL` is built in Phase 2. The other two need no new tables, which is
the whole point of shaping the table once.

**"Persistent / retained message storage" (from Q11).** In MQTT, a normal
message is delivered only to clients subscribed *at that moment*. A **retained**
message is stored by the broker and handed to every future subscriber of that
topic immediately on subscribe. Think of a noticeboard: the last notice stays
pinned until someone replaces it.

We use it for one thing: `devices/{slot_id}/desired` holds that gateway's
current configuration. So a gateway that was switched off, unplugged, or broken
for a week receives its configuration the instant it reconnects — no queue, no
expiry, no "the command was sent while you were offline" problem. The storage
cost is one small JSON document per device: roughly 1–4 KB, so about 4 MB for
1 000 gateways, held in the broker. That is the entire reason offline
convergence needs no code.

**"Which devices failed to receive the new settings" (from Q11).** You asked
for this to be visible, and it is the natural companion to retained storage:
retained delivery guarantees the device *will* get the config eventually, so
the operator's real question is *"which ones haven't, yet?"*. That is the
four-state view in §5.3 — `APPLIED` / `PENDING` / `STALLED` / `FAILED`, derived
from `desired_config_version` vs `applied_config_version` plus the last publish
result. It is one endpoint (`GET /provisioning/config-state/`), and it is what
turns "installed but never came online" from a customer complaint into a row
on a screen.

---

## 13. What I recommend you approve

1. **Phases 0–1** as a single unit of work. They are purely additive, touch no
   existing behaviour, and unblock the org-less device identity that Phase 1
   of your business flow requires.
2. **The retained desired-state model** (AD-3/AD-4) over a command-with-retries
   model. It is the single decision that makes offline, duplicate, and
   resync handling disappear rather than need code.
3. **Not reusing `CommandLog`** and **not unifying device credentials with
   Django's hashers** — both would look like reuse and cost more than they
   save.
4. **Deferring Phase 3** (imperative commands) until the config path is live,
   because the config path is what actually delivers the business value and
   the command path is only needed for rotation and diagnostics.

Three items from your answers deserve explicit sign-off because they are the
hardest to reverse:

| Item | Why it needs a conscious "yes" |
|---|---|
| **Installers as platform staff** (AD-14 / §5.6) | It adds a second audited cross-org access path to the system. Every future security review will ask about it. If installers could instead be given an `OrgMember` row in each served org, the access model stays single — but then the customer sees your technicians in their member list, and an org with no installer cannot be served. |
| **Removing farm-manager approval** (AD-17) | It deletes a documented control (D18) and retires the `PENDING_APPROVAL` path for new devices. The replacement (`applied == desired`) is a technical gate, not a human one, so the guarantee changes from "the customer accepted it" to "it is configured". |
| **`installation_jobs` as a new table** (AD-16) | It is the right model for multi-gateway farms, but it is a new org-scoped, RLS-enrolled table — the kind of addition that is cheap now and expensive to retrofit after data exists. |
| **Binding by QR claim code** (AD-18) | It moves a step onto the factory line: the label must carry `unit_id` + `claim_code`, and the claim code must be treated as single-use. Get this wrong at manufacturing and every unit needs a re-issued label. |

No code has been changed. `AGENT_HANDOFF.md` at the repository root carries
the same state in the handoff format, for whoever picks this up next.
