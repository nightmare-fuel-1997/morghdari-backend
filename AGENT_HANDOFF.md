# AGENT_HANDOFF — MQTT-native device provisioning

> **Read this file first, in full, before touching any code.**
> It is self-sufficient by design: it does not assume access to any prior
> conversation, transcript, or agent memory. Everything a successor needs to
> continue is here or is pointed at by an exact path.
>
> **Last updated:** 2026-10-18 (Phases 0–1 implemented)
> **Work state:** Discovery, architecture and planning complete. **Phase 0
> (foundations) and Phase 1 (hardware inventory) are IMPLEMENTED** — Phase 0
> is purely additive, Phase 1 adds the org-less factory panel and the authn
> query. Phase 2 (request → assign → bind) is next.

---

## Project Goal

Replace the gateway's HTTP dependency with a fully MQTT-based provisioning and
installation flow, with minimum disruption to the existing system.

The end state:

1. A gateway is registered by the Hardware Team at the factory with a permanent
   identity and a broker credential, before any farm or organization exists.
2. An organization submits an installation request; support approves it and
   routes it to the installer who covers that province.
3. The installer binds the physical device, registers halls and sensors, and
   submits.
4. The backend delivers the complete configuration to the gateway **over
   MQTT**, and the gateway confirms it applied it.
5. Administrative panels keep using REST. Only the gateway↔backend leg is
   MQTT-only.

**Hard constraint from the owner:** architectural correctness, security, reuse
of existing capability, and low-risk migration take priority over speed of
implementation. Do not start coding before the design is approved.

**Full analysis:** `docs/mqtt-provisioning-redesign-v1.md` (read §1, §3, §5
and §9 at minimum).

---

## Current Architecture

Confirmed by reading code, not docs. Tags: **[IMPL]** implemented and
reachable · **[CONTRACT]** documented only · **[MISSING]** absent.

### Identity and credentials

- Device credential row: `MqttDeviceCredential`
  (`backend/apps/mqtt_auth/models.py:18`). `device_id` is unique and equals
  `gateway_slot_id`; `username` unique; `password_hash` is **raw bcrypt
  `$2b$`**; plus `pending_password_hash`, `pending_since`, `is_active`,
  `last_rotated_at`, `expires_at`. **[IMPL]**
- Hashing: `hash_password()` (`backend/apps/mqtt_auth/services.py:49`) calls
  `bcrypt.hashpw` directly. `verify_password()` (`:56`) also accepts legacy
  Django wrapper strings.
- `PASSWORD_HASHERS` (`backend/config/settings/base.py:207`) governs **human
  users only** (Argon2 → PBKDF2 → bcrypt_sha256). It must never be used for
  device credentials: EMQX cannot parse any of those formats.

### Broker

- `emqx/emqx.conf:86-104` — postgres authn, bcrypt, single SQL query that
  **JOINs `gateway_slots`** and requires
  `provisioning_status IN ('PENDING_INSTALLATION','PENDING_APPROVAL','PROVISIONED')`.
  A credential without a slot row cannot authenticate.
- `emqx/emqx.conf:109-130` — authz from `mqtt_acl_rules` by username,
  `no_match = deny`, `deny_action = ignore`, allow-rows only.
- TLS 8883 only; plaintext listener disabled (`:35-37`).
- `gateway_slots` is FORCE RLS; `emqx_auth` gets a SELECT-only exemption via
  `backend/apps/tenants/migrations/0003_emqx_auth_read_policy.py` (twin of
  `initdb/02_rls.sql:96-110`).

### Topics

- T1 telemetry and T2 status are subscribed
  (`backend/morghdari/subscriber/main.py:41-42`). **[IMPL]**
- T3–T9 are **[CONTRACT]** only — see "Command channel" below.
- ACL generator: `gateway_acl_rule_items()` (`backend/apps/provisioning/api.py:63`)
  emits the full T1–T9 set (5 publish + 4 subscribe).

### Provisioning lifecycle

`transition()` (`backend/apps/provisioning/services.py:57`) +
`_transition_guarded()` (`:98`) with an explicit source-state matrix.
Statuses: `PENDING_SYNC, PENDING_INSTALLATION, PENDING_APPROVAL, PROVISIONED,
REJECTED, SUSPENDED` (CHECK at `backend/apps/devices/models.py:61-69`).

```
GatewayRequest.PENDING
  → review APPROVE  (api.py:482)  → GatewaySlot + credential + ACLs + EMQX user
                                    status PENDING_INSTALLATION
  → installer sync  (api.py:602)  → zones + pack mappings → PENDING_APPROVAL
  → manager approve (api.py:793)  → PROVISIONED (+ drain pending telemetry)
  → manager reject  (api.py:838)  → REJECTED (+ DLQ + ACL revoke)
  → suspend/resume  (api.py:880/921)
```

A `GatewaySlot` is created in exactly one place in the whole codebase:
`backend/apps/provisioning/api.py:524` (plus the subscriber seed command).

### Config delivery

`GET /api/v1/gateways/{gateway_slot_id}/config/` (`api.py:1039`), HTTP Basic,
payload from `gateway_config_payload()`
(`backend/apps/mqtt_auth/services.py:77`). Deliberately omits sensor slot ids
and the plaintext password. **This is the HTTP dependency to remove.**

### Command channel — the structural void

- **No backend MQTT publisher exists.** `paho-mqtt` is imported in exactly one
  file: the inbound subscriber (`main.py:30`). No module under `backend/apps/`
  imports it.
- `django-backend` broker principal is **[CONTRACT]** (`MQTT Topic Map` §3.3),
  never seeded. Only the two subscribers are seeded
  (`seed_subscriber_credentials.py:88`).
- `add_dev` / `set_label`: **[CONTRACT]** only. Zero occurrences in code.
  Planned as v0.5 task E6
  (`backend_docs/versions/v0.5-provisioning-zero-touch/04-step-by-step-implementation-tasks.md:85`),
  never shipped.
- Subscriber does **not** subscribe to `commands/response`.
- `CommandLog` (`backend/apps/auditing/models.py:15`, table `command_logs`,
  already RLS-enrolled) is **dead code** — no writers, no readers. Unusable
  here: `saloon_id` NOT NULL, `actuator_type` CHECK is actuator-only, statuses
  are `SENT|ACKNOWLEDGED|SUCCESS|FAILED`, and it carries `legal_hold`
  retention semantics.

---

## Confirmed Findings

All verified by reading the cited line. Do not re-litigate without new evidence.

| ID | Finding | Evidence |
|---|---|---|
| F1 | Backend has zero outbound MQTT capability | `grep -rn "import paho" backend/` → only `morghdari/subscriber/main.py:30` |
| F2 | `add_dev` has no implementation anywhere | `grep -rn "add_dev"` over the repo returns only `.md` files |
| F3 | Subscriber consumes only 2 lanes (telemetry, status) | `main.py:41-42`, `:147` |
| F4 | `CommandLog` has no writers/readers | `grep -rn "CommandLog" backend/apps backend/tests` → only the model |
| F5 | No sensor-slot creation path exists | no `SaloonSensorSlot.objects.create` in `backend/apps/` |
| F6 | Saloon creation is manager-only | `backend/apps/farms/api_follow.py:439` |
| F7 | Hardware registration requires an existing slot in an org-scoped support session | `backend/apps/accounts/api_hardware.py:70`, `accounts/views.py:710-732` |
| F8 | Device passwords are bcrypt `$2b$`, never plaintext | `mqtt_auth/services.py:49`, `models.py:24` |
| F9 | `GatewaySlot` is only ever created at request approval | `provisioning/api.py:524` |
| F10 | **Gateway status/LWT messages are dead-lettered** — `_on_message` sent every message through `handle_message`, whose gate 1 rejects non-`/telemetry` topics; `_handle_status` was defined and never called | `main.py:299-315` etc. | **FIXED 2026-10-18** — `tests/test_m10_02_status_lane.py`. No gateway online/offline state existed before. |
| F11 | `_handle_status` had three further bugs: it looked the gateway segment up in `SaloonSensorSlot` (wrong identity), tested lowercase statuses the contract sends uppercase, and was handed the paho client where it needed Redis | `main.py` `_handle_status` | **FIXED 2026-10-18**, all four defects in one patch. |
| F12 | ACL topics f-stringify `slot.farm_id`, which is nullable → `farms/None/...` | `provisioning/api.py:544`, `services.py:155` |
| F13 | ACLs are not regenerated when a slot's farm changes | written once at `api.py:544`; regenerated only on resume `services.py:147` |
| F14 | Sensor registry has no uniqueness on `hardware_id` | `devices/models.py:166-174` (index on `slot` only) |
| F15 | Rotation commit is human-mediated via `POST /rotation-ack/`, not device-mediated via MQTT `SUCCESS` | `api.py:1174` vs `MQTT Topic Map` §1.3.3 |
| F16 | `pack_id` format contradicts itself across docs: 8-hex (`Topic Map` §1.3.1) vs `"P-A"` (`Firmware Spec` §3.2) | see both |

F10–F11 are **new** (not in `BUGS.md`) and should be filed there regardless of
this project's outcome.

### Prior-art audit — most of the command channel was specified, then dropped

Do **not** re-design these; implement them. Full detail in design doc §1.4.

| Capability | Specified at | State |
|---|---|---|
| Publisher client | v0.5 tasks **E1**, `versions/v0.5-provisioning-zero-touch/04-step-by-step-implementation-tasks.md:65` — exact path `apps/mqtt_auth/publisher.py` | not written |
| `send_command()` + lifecycle | v0.5 **E2**, same file `:69` | not written |
| Command state machine | `Implementation Blueprint — How to Build.md:331-338` §11.2 | documented only |
| Rotation beat + device-mediated commit | v0.5 **E3/E4** `:73-79` | half-built (REST-triggered only) |
| `add_dev`/`set_label`/`REFRESH_CONFIG` senders | v0.5 **E6** `:85` | not written |
| `django-backend` principal | `MQTT Topic Map` §3.3, `Security Architecture.md:350` | documented, never seeded |
| Command-response lane | implied by E2; ACL row in Topic Map §3.2 | not subscribed |
| v0.5 acceptance tests | v0.5 `05-testing-and-acceptance-criteria.md:66-76,85` | **none exist** |

Why it disappeared: `Open Items Tracker.md:371` deferred it "→ v0.6 command
channel", but `versions/v0.6-rest-websocket/*` contains **zero** occurrences of
`command`/`publisher`/`downlink`. The deferral target never picked it up and no
later entry re-homed it.

**Three unresolved contradictions in the corpus** (these were disagreed upon,
not predicted):

| # | Contradiction | Effect |
|---|---|---|
| X1 | `Actor Use-Case & Dependency Map.md:52` — Hardware Team is "not scoped to any org or farm", and `:433` names it the writer of `gateway_slots`. `Implementation Blueprint — How to Build.md:316` — the slot is inserted by "gateway_request review APPROVED (platform side)". | Code followed the Blueprint (`provisioning/api.py:524`), forcing an org-less actor into an org-scoped gate (`accounts/views.py:710-732`). **RESOLVED 2026-10-18:** owner confirmed the Hardware Team is platform staff, so the Actor Map wins and the Blueprint rule is superseded for factory registration. |
| X2 | `Saas.md:1192` — "Hardware team logs the device's factory serial and token secret via the Django API" with no org. | Unimplementable: `gateway_slots.org_id` is NOT NULL. This is the seam the new flow lands on. |
| X3 | `Actor Use-Case & Dependency Map.md:437` — `saloon_sensor_slots` written by "Device Installer (via provisioning sync)". | `provisioning_sync` only validates (`api.py:705-709`); no create path exists. |

**Genuinely new (no prior art):** org-less factory inventory (`device_units`),
retained desired state / `config_version`, the `devices/{slot}/…` namespace,
installer work assignment (`gateway_requests` has no assignee —
`database.md:131-142`), and a sensor-slot creation endpoint.

Also note: `Actor Use-Case & Dependency Map.md:100` already carried a
**"Gap:"** note about validating no active `hardware_registry` row before
binding — i.e. defect D6 was written down as a known gap and never closed.

---

## Architectural Decisions

### Answers received from the owner (2026-10-18)

| Question | Answer | Landed as |
|---|---|---|
| Who is the Hardware Team? | Employees of the software company — platform staff, not customer staff | AD-13 |
| Who is an installer? | Also a member of the software company | AD-14 — job-scoped `installer_session` |
| How is work routed to an installer? | Support assigns a specific installer by hand, after approving | AD-11 |
| Does the request create the farm? | Yes | AD-15 |
| One request = one gateway or several? | Several — a request is for one farm | AD-16 — `installation_jobs` |
| Does farm-manager approval stay? | Remove it | AD-17 — technical gate replaces it |
| May the installer create halls? | Yes | AD-8 amended |
| Sensor id format | 8 hex digits (4 bytes) | `pack_id` = 8 lowercase hex |
| `username == slot_id`? | Yes | unchanged |
| Photos | Per sensor as well | AD-20 — `device_photos` |
| May an installer decline a job? | No | no accept/decline state |
| Node id printed on the sensor? | Yes, and entered at mapping time | AD-23 — `pack_id` mandatory |
| What does the installer scan? | The username from the label/QR | AD-18 — QR carries `unit_id` + single-use `claim_code` |
| Reassign after bind? | Yes — and cover repair/maintenance too | AD-21 — `installation_jobs.kind` |
| Show devices that missed their settings? | Yes, in the frontend | AD-22 — four-state convergence view |
| May we assume an LED on the device? | **No** | AD-18 rewritten: binding needs no device behaviour |

**No blocking questions remain.** All clarifications are closed (design doc
§12.2).

Each decision records the alternative that was rejected and why, so a successor
does not "simplify" it back.

### AD-1 — Org-less factory registry `device_units` (not nullable `GatewaySlot.org`)

A device must exist and authenticate before any org/farm. `GatewaySlot.org` is
NOT NULL and the table is FORCE RLS.

**Chosen:** a new org-less table `device_units`, mirroring the existing posture
of `mqtt_device_credentials` / `mqtt_acl_rules` (org-less precisely because the
broker reads them).

**Rejected:** (a) making `org` nullable — breaks the RLS `WITH CHECK`, forces
inventory writes onto the BYPASSRLS admin alias, invalidates "a slot always
belongs to an org" which many code paths assume; (b) parking unbound devices in
a synthetic Platform org — pollutes a tenant namespace and makes binding a
cross-tenant move.

### AD-2 — `unit_id` == the eventual `slot_id`

The factory assigns the permanent logical identity. Binding is a pure
server-side link (`GatewaySlot(slot_id=unit_id, org, farm)`), never a credential
reissue. This is already what the firmware spec promises (§2 step 5).

**Consequence:** `mqtt_device_credentials.device_id` is written once, at the
factory, and never changes. No reflash on binding.

### AD-3 — Retained desired state, not queued commands

Provisioning is convergence, not delivery. D1 `devices/{slot_id}/desired` is
**retained**; the device subscribes and is immediately in sync — offline,
duplicate, expired-session, and resync problems all disappear.

**Rejected:** fire-and-forget command with retries — inherits every
queue-expiry and ordering problem and still cannot answer "did it apply?".

### AD-4 — `config_version` integer for idempotency

Monotonic per slot. Device applies only if `config_version > applied`. Ordered
and comparable, no uuid ring buffer needed for the config path. `command_uuid`
remains for imperative T3 commands (with a small flash ring buffer).

### AD-5 — New `gateway_commands` table; `CommandLog` untouched

`CommandLog` is saloon-scoped, actuator-only, dead, and carries `legal_hold`
retention semantics. Widening it would be a destructive migration for no gain.

**Note:** the v0.5 plan *did* intend `command_logs` as the journal (E2 says
"`command_logs` INSERT (SENT)"; Blueprint §11.2 is titled "Command lifecycle
(`command_logs.current_status`)"). This decision overrides that deliberately —
reuse the §11.2 **state matrix** verbatim, change only the storage.

### AD-6 — Publish via paho from Celery, not EMQX's HTTP publish API

`POST /api/v5/publish` is an admin path that **bypasses per-topic ACL** — the
exact control this work strengthens. Keep it as a documented ops fallback only.

### AD-7 — Firmware owns the topic grammar; backend supplies only `org_id`/`farm_id`

Pure firmware-derivation is impossible (org/farm are server facts and can
change). Pure backend-supplied literal topics discard the only cheap on-device
integrity check. Hybrid: the firmware composes
`{org}/farms/{farm}/gateways/{slot}/{suffix}` and rejects a document whose
`topics` block disagrees with its own derivation.

### AD-8 — New device-rooted namespace `devices/{slot_id}/…` for the control lane

**Rejected:** `{org}/farms/{farm}/gateways/{gw}/config` — an unbound device has
no org, so it cannot compute its own topic (chicken-and-egg), and a farm move
would strand it between unsubscribe and resubscribe.

**This is a deliberate, documented exception to "org_id prefixes every topic"**
(`MQTT Topic Map` §6). That rule exists so the first topic segment is
unforgeable org evidence for **ingested telemetry**; D1/D2 carry no telemetry.
The ACL compensates: each username is pinned to one literal topic, no
wildcards. Record the amendment in `MQTT Topic Map` §6 during Phase 4.

### AD-9 — Installer role widened for hall/position creation, gated by `AccountRoleScope`

Reuses the existing scope-narrowing engine
(`backend/apps/accounts/permissions.py:76`) rather than inventing
installer-specific endpoints.

### AD-10 — Sensor admission enforced at both ends

Gateway drops unauthorized packs and reports `rejected_packs`; the server also
enforces `UNIQUE(hardware_id) WHERE removed_at IS NULL` on the sensor registry.
Gateway-side filtering alone is bypassable by a compromised gateway.

### AD-11 — Support assigns a specific installer by hand (owner decision 2026-10-18)

No province/city routing engine. Assignment is one FK on `gateway_requests`
plus one endpoint. A routing engine would need a coverage map, conflict rules,
and an uncovered-province fallback — none of which exist — and support already
reviews every request, so the human is already in the loop.
`AccountRoleScope.province` stays useful as a **filter on the picker**, not as
an algorithm.

### AD-12 — Assignment lives on `gateway_requests` (one request = one job)

**SUPERSEDED by AD-16** (owner: one request may cover several gateways).
Kept here so a successor does not "simplify" back to it: assignment must be
per-gateway, because a farm legitimately needs more than one gateway and each
one is bound to a different physical unit by possibly a different installer.

### AD-13 — The Hardware Team is platform staff, not tenant staff (owner decision 2026-10-18)

They belong to the software company. Factory endpoints therefore gate on
`is_platform_staff` + `staff_role`, and must **not** reuse
`_support_op_context` (`accounts/views.py:710`), which demands an org-scoped
session. This resolves corpus contradiction X1 in favour of the Actor Map.

**Consequence to remember:** review-approval must STOP minting the slot,
credential, and ACL rows (`provisioning/api.py:519-552`), because the device
already exists at the factory. Slot creation moves to the bind step. This is
the most delicate edit in the plan — every existing approved request must keep
working.

### AD-14 — Installers are platform staff; tenant access via a job-scoped session

Owner: "the installer is a member of our organization". They therefore have no
`OrgMember` row, no `org_id` claim, and middleware branch ② deliberately gives
platform staff zero rows (`tenants/middleware.py:70-76`).

**Reuse:** the audited support session (`accounts/views.py:550-615`) already
issues a 15-minute token with `org_id` that middleware branch ① turns into an
RLS-scoped session. Clone its shape as `installer_session` with claims
`org_id + farm_id + job_id`.

**Do NOT reuse the `support_session` claim itself** — holding it also opens
`_support_op_context`, which unlocks gateway suspend/resume and alert
force-resolve. Installers must not inherit support-operator powers.

**Two mandatory details:**
- `provisioning/api.py:611` (`"installer" in roles`) must accept the
  `installer_session` claim, or every installer endpoint 403s.
- Every installer endpoint must filter by the claim's `farm_id` **explicitly**.
  `scoped_farms()` treats zero `AccountRoleScope` rows as "unnarrowed"
  (`accounts/permissions.py:76`), so a platform installer would otherwise see
  every farm in the org.

Full design: design doc §5.6.

### AD-15 — The install request creates the farm

`gateway_requests.farm_id` becomes nullable; add `farm_spec` JSON
(`{name, province_id, city_id, village_id?}`) and `expected_gateways`. Support's
approval materialises the `Farm` owned by the requesting org in the same
transaction that creates the jobs.

**Corpus rule amendment:** "the Organization Owner creates farms/saloons"
(`Actor Use-Case & Dependency Map.md:114`, `:429`) becomes "the Owner requests,
the platform materialises". Record it in that document during Phase 4.

### AD-16 — One request → N `installation_jobs`

`Saas.md` already says a gateway owns a *zone* of saloons, not a whole farm, so
multi-gateway farms are normal. Each job carries its own assignee, device unit,
and gateway slot. Approval creates `expected_gateways` jobs (default 1).

New org-scoped, RLS-enrolled table: `installation_jobs`
(`kind`, `request`, `org`, `farm`, `assigned_installer`, `assigned_by`,
`assigned_at`, `state`, `gateway_slot`, `unit`, `created_at`).
States: `PENDING → ASSIGNED → BOUND → SUBMITTED → COMPLETED`, plus `CANCELLED`.
`kind ∈ {INSTALL, REPAIR, MAINTENANCE}` — only `INSTALL` is built in Phase 2,
but shape the column now (AD-21).

### AD-17 — Farm-manager approval removed; replaced by a technical gate

Owner: "it just adds unnecessary complexity". The D18 gate existed so an
installer could not approve their own install; installers are now our own
staff, so it oversaw employees rather than protecting the customer.

**Replacement:** the slot reaches `PROVISIONED` automatically when
`applied_config_version == desired_config_version` **and** the job is
`SUBMITTED`. Enforce it in the reconciler, not the UI.

**Consequences:**
- `PENDING_APPROVAL`, `approve_gateway`/`reject_gateway`, and
  `telemetry:pending_approval` stay in the code for legacy slots. **Do not
  delete them** — a future tenant may want the gate back.
- Automatic transitions need an actor: `provisioning_audit_log.changed_by` is a
  non-null FK (`provisioning/models.py:21`). Add a `system` user via data
  migration + `get_system_user()`. Do **not** make the column nullable.

### AD-18 — Bind by scanning a single-use QR claim code; no password, no device behaviour

The owner asked: "if the installer only has the username and doesn't have the
password, how can they add the device?" **They should not need it** — and the
device must not be assumed to have an LED or any other indicator ("how the
device works isn't important to us").

**Design:** the factory prints a QR encoding `unit_id` + `claim_code`. The
claim code is a separate, single-use secret, bcrypt-hashed at rest, destroyed
on use, and *not* the MQTT password. Binding is then a pure server-side
verification:

```
POST /provisioning/installations/{job_id}/gateway/ {unit_id, claim_code, photo?}
```

No device interaction, no connectivity requirement, no firmware feature. The
MQTT password never leaves the factory. Activation is the device's job: it
connects with its credential and reports `APPLIED`.

**Why not just the username?** A username on a label is not a secret — anyone
who photographs the unit can claim it. The claim code proves possession of the
physical label while staying useless after first use.

**Lost or damaged label:** `POST /platform/devices/units/{id}/claim-code/`
re-issues (rotates the hash); if the hardware team no longer has the unit,
support uses `POST …/{job_id}/gateway/override/` with a mandatory reason.

### AD-21 — Reassignment at any state; jobs cover repair and maintenance

Owner: "assigning the device installation and maintenance and repair process to
another installer". `installation_jobs.kind ∈ {INSTALL, REPAIR, MAINTENANCE}`.
Reassignment is allowed before **and** after bind; after bind, record
`reassigned_after_bind` in the audit row and keep the original installer in the
job history. No "release device" ceremony — the device is at the farm, not with
the installer.

### AD-22 — Four-state configuration convergence view

Owner asked for a way to see which devices did not receive their settings,
visible in the frontend. Derived, never stored:

| State | Condition |
|---|---|
| `APPLIED` | `applied == desired` |
| `PENDING` | `applied < desired`, last publish OK, within `config_apply_timeout_sec` |
| `STALLED` | `applied < desired`, last publish OK, timeout elapsed (includes "never reported") |
| `FAILED` | last publish failed, or the device reported `FAILED` |

`GET /provisioning/config-state/?state=pending|stalled|failed`, with
`last_publish_error` and `last_reported_at` on each row. `STALLED` is the one
that matters operationally: it is "installed but never came online".

### AD-23 — `pack_id` (8-hex node id) is mandatory and printed on the sensor

Owner asked whether the node id should exist by default and be printed. **Yes,
both.** It is the join key, not metadata: the gateway filters LoRa traffic by
it, telemetry carries it, and `GatewaySensorMapping` resolves it to a slot. No
mapping ⇒ every reading dead-letters as `UNKNOWN_SLOT`.

It is burned at manufacture and read from the label; the installer records it
and never invents it.

**Open sub-point for the hardware team:** sensors currently have two
identifiers — `pack_id` (node id, gateway-facing) and
`HardwareRegistry.hardware_id` (vendor serial). Maintaining both invites them
to disagree. Recommendation: the installer enters only the node id;
`hardware_id` stays optional/informational.

### AD-19 — Category A commands move to `devices/{slot}/command`

Category A (identify, rotate, diagnostics, reboot) must work **before** the
device has an org, and T3 is org-prefixed. Category B (actuators) stays on
T3/T4 because it is saloon-scoped and only runs after provisioning.

Costs nothing today — T3 has no publisher. All device-originated events,
including command ACKs, arrive on `D2 reported`.

### AD-20 — `device_photos` table

Owner: each sensor is an independent entity and needs its own photo.
`SaloonPhoto` is saloon-scoped only (`devices/models.py:216`). New org-scoped,
RLS-enrolled table with `gateway_slot` and `sensor_slot` nullable FKs and a
CHECK that exactly one is set. `SaloonPhoto` is untouched.

---

## Completed Tasks

- **Phase 1 — Discovery.** Complete. Read and cross-checked: devices /
  mqtt_auth / provisioning / telemetry / accounts / farms / auditing / tenants
  apps; the subscriber package; `emqx/emqx.conf`; `initdb/{01_roles,02_rls}.sql`;
  the docs corpus (`MQTT Topic Map`, `Gateway Firmware Specification`,
  `design-zero-touch-provisioning`, `database.md`, `Open Items Tracker`);
  `openapi.yaml` routing. Every claim in this file carries a path.
- **Phase 2 — Architecture & gap analysis.** Complete.
  16 gaps catalogued (G1–G16), 10 decisions recorded (AD-1…AD-10), both
  architectural questions answered with rejected alternatives.
- **Prior-art audit** (owner-requested follow-up). Complete. Swept the whole
  `backend_docs` corpus for prior design of every "missing" capability. Result:
  v0.5 Phase E (E1–E6) + Blueprint §11.2 specify the command channel in full
  and were dropped between v0.5 and v0.6; three corpus contradictions
  (X1–X3) were found unresolved. Recorded in design doc §1.4 and above.
- **Phase 3 — Implementation planning.** Complete. 4 phases, ~30 tasks, each
  with exit criteria; impact analysis enumerates new and modified files with a
  risk rating; test strategy names every test file and scenario.
- **Phase 0 — Foundations.** **IMPLEMENTED 2026-10-18, tests green.** Every task
  below is done and verified by `tests/test_m10_00_foundations.py` (27 tests):
  1. `device_units` table (org-less, no RLS) — AD-1/AD-2/AD-18.
  2. `installation_jobs` table (org-scoped, RLS) — AD-16/AD-21.
  3. `gateway_commands` table (org-scoped, RLS) — AD-5.
  4. `device_photos` table (org-scoped, RLS, exactly-one-target CHECK) — AD-20.
  5. `gateway_slots` convergence columns — AD-3/AD-4/AD-22.
  6. `gateway_requests.farm_spec` + `expected_gateways` — AD-15/AD-16.
  7. System user + `get_system_user()` — AD-17.
  8. `apps/mqtt_auth/publisher.py` (v0.5 **E1**, at the planned path).
  9. `apps/provisioning/topics.py` — D1/D2/D3 grammar (AD-19).
  10. `apps/provisioning/desired_state.py` — the payload builder.
  11. `apps/provisioning/tasks.py` — publish / reconcile / convergence sweep,
      including the `applied == desired` activation gate.
  12. `seed_publisher_credentials` management command (the `django-backend`
      principal that had never been seeded).
  13. `installer_session` claim + shared issuer + middleware branch ①b — AD-14.
  14. RLS enrolment in `initdb/02_rls.sql` + `tenants/0006`, `emqx_auth` grant
      on `device_units`, `database.md` DDL, parity-script update.

- **Phase 2 slices (d)+(e) — submit and the four-state view.** **IMPLEMENTED 2026-10-19.**
  `POST /provisioning/installations/{job_id}/submit/` completes the no-approval
  activation path: BOUND → SUBMITTED, then AD-17's existing `_maybe_activate`
  fires the moment `applied == desired`, so the slot reaches `PROVISIONED` with
  no manager in the loop. It requires a bound unit, is idempotent, and — the case
  that would otherwise wait forever — re-runs the activation check on submit, so
  a device that reported BEFORE the installer pressed Done still converges.
  `GET /provisioning/config-state/` exposes the derived four-state view (AD-22),
  with `last_publish_error` surfacing as `FAILED`. Contract → v1.11.0.

- **Phase 2 slice (c) — the claim-code bind.** **IMPLEMENTED 2026-10-19.**
  `POST /provisioning/installations/{job_id}/gateway/` joins a factory unit to a
  job by `unit_id` + claim code in ONE transaction: creates the `gateway_slots`
  row (slot id = unit id, AD-2), **expands** the ACL from the 3-row device lane
  to the full T1–T9 tenant set (3 rows kept + 9 added = 12), consumes the claim
  code, moves the job to BOUND, bumps `desired_config_version` (AD-4) and queues
  a PROVISION `GatewayCommand`. Nothing talks to the broker, so it works with
  the unit OFFLINE; the retained document is pushed in `transaction.on_commit`,
  and the convergence sweep is the safety net if that fails.
  `POST …/gateway/override/` does the same for support with a mandatory reason,
  audited `via_override: true` in both ledgers — and installers may not use it.
  Tests: `test_m11_03_installation_jobs.py` (25, incl. claim-code single use,
  replay idempotence, wrong code, second-job conflict, offline bind, retired
  unit). Contract → v1.10.0.

- **Phase 2 slice (b) — assignment, queue, installer session.** **IMPLEMENTED 2026-10-19.**
  `apps/provisioning/installations.py` — five endpoints under
  `/provisioning/installations/`: the queue (`install_jobs`), the job detail,
  the assignable picker (regional scope via `account_role_scopes`), the
  assignment (`install_job_assign`, support/admin only) and the scoped job
  session (`install_job_session`). The middleware branch ①b built in Phase 0
  finally has a caller. All reads are admin-alias discovery + per-org
  `tenant_context`, because the installer is org-less and FORCE RLS would
  otherwise show them nothing. Tests: `test_m11_03_installation_jobs.py` (17).
  Three bugs the tests caught while building it: an installer was shown the
  WHOLE org queue; an unassigned job answered 403 where 409 is honest; support
  was blocked from opening a session for an installer whose phone died (now
  allowed, and audited).

- **Phase 2 slice (a) — request → approve → farm + jobs.** **IMPLEMENTED 2026-10-19.**
  `gateway_requests.farm_spec` + `expected_gateways` are now validated by
  `GatewayRequestCreateSerializer` (the POST view used to read `request.data`
  raw, so "exactly one of farm_id / farm_spec" was unenforceable), and
  **approval no longer mints the slot, credential, or ACL rows** — it
  materialises the `Farm` from `farm_spec` and bulk-creates one
  `InstallationJob` per `expected_gateways`. The approval response therefore
  carries no password, which is the point: there is nothing to leak.
  R9's compatibility promise holds because the whole edit is one transaction
  and a request can only be approved once (409 after).
  Two `Farm` field errors were caught by the new tests: it has neither `notes`
  nor `created_by`.
  Tests: `test_review_approval_creates_jobs_and_no_slot` (admin + support),
  `test_review_approval_materialises_the_farm_from_farm_spec`,
  `test_request_body_is_validated` (9 param cases).

- **Fleet protocol pin — MQTT 3.1.1 only (hardware-team limit, owner 2026-10-19).**
  Every client already pinned `mqtt.MQTTv311` (publisher, subscriber,
  `firmware_sim`, the HIL harness); the constraint is now also a BROKER rule:
  `zone { mqtt { min_protocol_version = 3 / max_protocol_version = 4 } }` in
  `emqx/emqx.conf`, mirrored as `EMQX_ZONE__DEFAULT__MQTT__*` in the CI broker
  service, and proven by `test_mqtt_5_is_rejected_while_311_is_accepted` against
  a live broker. Note for precision: "EMQX 3.1.1" conflates the broker (EMQX 5.5)
  with the wire protocol (MQTT 3.1.1); the pin is on the PROTOCOL.

- **Phase 1 — Hardware inventory.** **IMPLEMENTED 2026-10-18.**
  - `PlatformAuditLog` (org-less, `apps/auditing`) — the Hardware Team's
    registrar had nowhere to write, because `account_activity_logs.org_id` is
    NOT NULL and there is no org yet.
  - `apps/accounts/platform_gate.py` — `is_platform_staff` + `staff_role`,
    deliberately NOT `_support_op_context` (which demands an org claim).
  - `apps/devices/api_units.py` + `serializers_units.py` — register (mints unit
    + credential + claim code + 3 ACL rows in one transaction), credential
    re-issue, claim-code re-issue, retire (deactivates + deletes ACL rows),
    inventory list at `/api/v1/platform/devices/units/`.
  - `bootstrap_acl_rule_items()` in `mqtt_auth/services.py` — the 3-row lane.
  - `emqx/emqx.conf` authn query now `LEFT JOIN`s `device_units` so an unbound
    factory unit can CONNECT (the first broker-wide edit; validated by
    `test_m10_01`'s Postgres-gated SQL test).
  - Tests: `tests/test_m10_01_device_units.py` (27) — incl. the assertion that
    an unbound unit's ACL holds NO tenant topic and NO wildcard.

- **Two pre-existing defects fixed** (both found during discovery, both signed
  off by the owner as fixable since this is not production):
  - **D1/D2/D2b — the gateway status lane was completely dead.** It was
    subscribed since v0.3 but never routed: messages fell into the telemetry
    parser, which rejects anything not ending in `/telemetry`, so every
    ONLINE/OFFLINE publish and every LWT dead-lettered as `bad_topic`.
    `_handle_status` had never been called, looked the gateway segment up in the
    wrong table, tested for lowercase statuses that the contract sends
    uppercase, and (D2b, caught by the regression test) would have received the
    paho client where it needed Redis. Now routed, writes
    `gateway:lwt:{org}:{slot}`, and still mirrors into the sensor-pack key for
    the demo topology.
  - **D3 — `gateway_acl_rule_items()` f-stringified a nullable `farm_id`,
    producing `farms/None`.** A farm-less slot — exactly what the new factory
    flow creates — would have received rules matching no topic. Now returns
    `[]` and the tenant rules are added at BIND time.
  - Tests: `tests/test_m10_02_status_lane.py` (15).

**Behaviour changed by Phase 1** (deliberately, and only in the factory path):
the authn query. Nothing else — no existing endpoint, model, or migration was
modified; the status-lane and ACL fixes are corrections to dead/broken paths.

**Behaviour is unchanged by Phase 0.** No endpoint, serializer, or existing
service was modified except the tenant middleware (one additive branch) and the
`emqx.conf` authn query — which is deliberately NOT touched yet; it is a Phase 1
task, so production broker behaviour is still exactly as before.

---

## Modified Files

| File | Change |
|---|---|
| `docs/mqtt-provisioning-redesign-v1.md` | **NEW** — the full analysis and design (13 sections) |
| `AGENT_HANDOFF.md` | **NEW** — this file |
| `backend/apps/auditing/models.py` | +`PlatformAuditLog` (org-less) |
| `backend/apps/auditing/migrations/0003_platform_audit_log.py` | **NEW** |
| `backend/apps/accounts/platform_gate.py` | **NEW** — org-less platform-staff gate |
| `backend/apps/devices/api_units.py` | **NEW** — the 5 factory endpoints |
| `backend/apps/devices/serializers_units.py` | **NEW** |
| `backend/apps/accounts/urls.py` | +5 `/platform/devices/units/*` routes |
| `backend/apps/mqtt_auth/services.py` | +`bootstrap_acl_rule_items()` |
| `emqx/emqx.conf` | authn query `LEFT JOIN device_units` |
| `docker-compose.ci.yml` | +`e2e-bootstrap` service; +`MQTT_HOST`/`MQTT_BOOTSTRAP_TLS` for the publisher; +`./emqx:/emqx:ro` on `test` |
| `docs/hardware-firmware-requirements-v1.md` | **NEW** — the document to send the firmware team |
| `scripts/firmware_sim.py` | **NEW** — independent device-contract implementation (no backend imports, by design) |
| `backend/tests/test_m10_03_bootstrap_lane_live.py` | **NEW** — 3 live-broker scenarios |
| `docker-compose.ci.yml` | per-service `mem_limit`/`cpus` caps (laptop-safe) |
| `backend/tests/test_m10_01_device_units.py` | **NEW** — 27 tests |
| `backend/tests/test_m10_02_status_lane.py` | **NEW** — 15 tests |
| `backend/morghdari/subscriber/main.py` | **status lane wired + 3 defects fixed** |
| `backend/apps/alerts/services/redis_keys.py` | +`gateway_lwt_key()` |
| `backend/apps/provisioning/api.py` | `gateway_acl_rule_items` returns `[]` for a farm-less slot |
| `backend/apps/devices/models.py` | +`DeviceUnit`, +`DevicePhoto`; +5 convergence columns on `GatewaySlot`; +`farm_spec`, `expected_gateways` on `GatewayRequest` |
| `backend/apps/devices/migrations/0002_phase0_devices_provisioning.py` | **NEW** — the above |
| `backend/apps/provisioning/models.py` | +`InstallationJob`, +`GatewayCommand`; `import uuid` |
| `backend/apps/provisioning/migrations/0002_phase0_devices_provisioning.py` | **NEW** — the above |
| `backend/apps/tenants/migrations/0006_rls_phase0_tables.py` | **NEW** — RLS on the 3 new org-scoped tables + `emqx_auth` SELECT on `device_units` |
| `backend/apps/accounts/migrations/0005_system_user.py` | **NEW** — the system actor |
| `backend/apps/accounts/system.py` | **NEW** — `get_system_user()` |
| `backend/apps/accounts/session_tokens.py` | **NEW** — shared scoped-session issuer (support + installer) |
| `backend/apps/mqtt_auth/publisher.py` | **NEW** — outbound MQTT publisher (v0.5 E1) |
| `backend/apps/mqtt_auth/management/commands/seed_publisher_credentials.py` | **NEW** — seeds the `django-backend` principal |
| `backend/apps/provisioning/topics.py` | **NEW** — D1/D2/D3 topic grammar |
| `backend/apps/provisioning/desired_state.py` | **NEW** — desired-state document builder |
| `backend/apps/provisioning/tasks.py` | **NEW** — publish / reconcile / convergence sweep |
| `backend/apps/tenants/middleware.py` | +branch ①b for `installer_session` (additive) |
| `backend/config/settings/base.py` | +1 beat entry (convergence sweep) |
| `backend/tests/test_m10_00_foundations.py` | **NEW** — 27 Phase 0 tests |
| `initdb/01_roles.sql` | +`GRANT SELECT ON device_units TO emqx_auth` |
| `initdb/02_rls.sql` | +3 tables in the RLS array |
| `scripts/check_rls_parity.py` | reads 0006 as well as 0002 |
| `docker-compose.prod.yml` | publisher env + certs mount on `celery-worker` |
| `.env.vps.example` | +`PUBLISHER_*`, +`MQTT_PUBLIC_HOST/PORT` |
| `backend_docs/database.md` | +Compartment 7b DDL for the four new tables and the column additions |

**Not touched on purpose:** `emqx/emqx.conf` (the authn-query change is Phase 1
and is the only broker-wide risk), every existing endpoint/serializer/service,
`command_logs`, `apps/telemetry/`, `apps/alerts/`, `apps/monitoring/`.

---

## Pending Tasks

Ordered by priority. Do not start a task until its predecessor's exit criteria
are met.

### Phase 0 — Foundations ✅ DONE (2026-10-18)

All twelve tasks are implemented and green — see "Completed Tasks" for the
per-task summary and `tests/test_m10_00_foundations.py` for the evidence.
Deviations from the plan worth knowing:

* Task 2 said to add the new tables to `tenants/0002_rls_policies.py`. **That
  file is frozen history** (its own docstring says so), so the RLS went into a
  new `tenants/0006_rls_phase0_tables.py` instead, and the parity script now
  reads both.
* Task 10's `farm_id → nullable` needed **no migration**: the column was already
  nullable; only the serializer requires it. That is a Phase 2 change.
* Task 6's file landed at the path the v0.5 plan named
  (`apps/mqtt_auth/publisher.py`), closing old task E1.
* Task 12 shipped the issuer + middleware branch only. The
  `POST …/{job_id}/session/` endpoint is Phase 2 (it needs `installation_jobs`
  to exist and an assignee to check).

**Exit criteria met:** behaviour unchanged; 27/27 Phase 0 tests green;
`makemigrations --check` clean. The live-broker half of the exit criterion
(a retained message reaching the subscriber counter) needs the Phase 1 authn
change and a running EMQX, so it is carried into Phase 1 verification.

### Phase 1 — Hardware inventory (unblocks org-less identity)

13. `POST /platform/devices/units/` — platform-staff, **org-less** gate (a new
    helper; do NOT reuse `_support_op_context`, it requires an org claim —
    see `accounts/views.py:710-732`).
14. `POST /platform/devices/units/{id}/credential/` — issue the MQTT password
    (plaintext once, to the factory line only) **and** the single-use
    `claim_code` that goes into the QR (AD-18).
15. `POST /platform/devices/units/{id}/claim-code/` — re-issue a lost label's
    code (rotates the hash; audited).
16. `POST /platform/devices/units/{id}/retire/`.
17. `GET /platform/devices/units/` — inventory with state filter.
18. `bootstrap_acl_rule_items()` — exactly 3 rows (subscribe `desired`,
    subscribe `command`, publish `reported`).
19. Authn query branch in `emqx/emqx.conf` (§7.1 of the design doc) + grant +
    `docker exec morghdari-emqx emqx check config`.

**Exit criteria:** a unit registers with no org, connects over TLS with its
factory credential, subscribes to its own `desired` and `command` topics, and
**cannot** publish to any tenant topic.

✅ **DONE (2026-10-18).** All seven tasks implemented and green. The
`emqx.conf` branch is the only broker-wide edit; it MUST be validated with
`docker exec morghdari-emqx emqx check config` on a rehearsal stack before it
goes near production (see R3).

### Phase 2 — Request → approve → assign → bind → submit → converge

19. **Change review-approval**: materialise the farm from `farm_spec`, create
    N jobs, and **stop minting the slot/credential/ACLs**
    (`provisioning/api.py:519-552`). **Highest-risk edit in the plan** —
    legacy approved requests must keep working.
20. `POST /provisioning/installations/{job_id}/session/` — installer job
    session (AD-14).
21. `GET /provisioning/installations/` — installer queue;
    `GET …/{job_id}/` — job detail.
22. `POST …/{job_id}/assign/` — support assigns; picker filtered by province.
    Reassignment allowed at any state (AD-21).
23. `POST …/{job_id}/gateway/` — atomic bind by `unit_id` + `claim_code`
    (verify → create slot → expand ACL → consume code → audit). Works with the
    unit offline (AD-18).
24. `POST …/{job_id}/gateway/override/` — support-only bind without a claim
    code, mandatory reason, always audited.
25. `POST /saloons/{id}/sensor-slots/` — the missing writer (F5).
26. `POST /saloons/{id}/sensor-slots/{slot}/photo/` — `device_photos` (AD-20).
27. `POST …/{job_id}/sensors/` — pack → sensor-slot mapping; `pack_id`
    (8-hex node id) is **required** (AD-23).
28. `POST …/{job_id}/submit/` — bump desired version, enqueue publish,
    job SUBMITTED.
29. `build_desired_state(slot)` — pure function, reuses the field names from
    `gateway_config_payload`.
30. Subscriber lane `$share/telemetry_workers/devices/+/reported` → Redis
    stream.
31. Reconciler: reported → `applied_config_version`; auto-`PROVISIONED` when
    applied == desired and job submitted (system actor).
32. Beat sweep: republish where `applied < desired`.
33. `GET /provisioning/config-state/?state=pending|stalled|failed` — the
    four-state convergence view (AD-22), with `last_publish_error` and
    `last_reported_at`.
34. Widen `saloon_create` / `position_create` to the installer session +
    `farm_id` filter (AD-8 amended).
35. Update `Actor Use-Case & Dependency Map.md` for the two amended rules
    (Owner-requests-farm, installers may create halls).

**Exit criteria:** request→approve→assign→session→bind→configure→submit→
converge completes end to end; `applied == desired`; the slot reaches
`PROVISIONED` with no human approval; gateway publishes on T1; duplicate submit
is a no-op; killing the gateway mid-flow converges without operator action;
a unit that never comes online appears as `STALLED` in the config-state view.

### Phase 3 — Command channel

> **Reuse, don't redesign.** Tasks 36–39 are v0.5 E1–E6 + Blueprint §11.2,
> already specified in detail (see "Prior-art audit"). Only the status-lane
> fix (40) is new. This phase is roughly half the size it first appeared.

36. Subscriber lane `+/farms/+/gateways/+/commands/response`.
37. `ROTATE_CREDENTIAL` end to end (closes F15).
38. `DIAGNOSTICS_REQUEST` end to end.
39. Expiry sweep + attempt counter.
40. Status-lane fix (closes F10/F11).

### Phase 4 — Cutover

41. `mqtt_config_enabled` → 1.
42. HTTP config endpoint gains `Deprecation` + `Sunset` headers.
43. Docs updated: `MQTT Topic Map` (§1.3, §6 amendment, D1–D3 rows),
    `Firmware Spec` (§2, §8.4, `pack_id` format), 
    `design-zero-touch-provisioning.md`, Actor Map rule amendments.
44. Retire the HTTP endpoint after one firmware generation.

### Backlog (independent of this project, but discovered here)

- File F10/F11 (dead status lane) in `BUGS.md`.
- Fix F14 (no uniqueness on active sensor `hardware_id`).
- Fix F12 (ACL `farms/None`) — becomes mandatory before Phase 1 ships.

---

## Known Issues & Risks

### Verified problems that will bite during implementation

| ID | Issue | Mitigation |
|---|---|---|
| R1 | `gateway_acl_rule_items` f-stringifies a nullable `farm_id` → `farms/None/...` | Make it return `[]` for a farm-less slot; assert in a regression test |
| R2 | ACLs are not regenerated when a farm changes | Regenerate inside the bind/rebind transaction |
| R3 | `emqx.conf` is the single source of truth and a mistake takes the broker down | `emqx check config` before every deploy; keep the old query as a comment; rehearse on the rehearsal stack first. **This risk is now live** — the authn query was changed in Phase 1. |
| R4 | `initdb/*.sql` runs **before** Django creates tables, so the same rules must also exist as a migration | Every RLS/grant change needs both halves — the project already does this (`tenants/0003` is the twin of `02_rls.sql`) |
| R5 | `CommandLog` looks like the right table and is not | Do not reuse it; see AD-5 |
| R6 | The subscriber is the ingestion hot path; a bad change silently kills telemetry | `SUBACK` denial is CRITICAL-logged and retried (`main.py:156-181`) — keep that behaviour; test new lanes' granted-QoS |
| R7 | Retained messages persist in EMQX across restarts and are delivered to every new subscriber | Only the device subscribes to its own D1 (literal ACL row); never add a wildcard subscribe for `devices/+/desired` |
| R8 | A `django-backend` credential can publish to any org's T3 | Documented accepted risk (`Topic Map` §3.3). Per-org publisher credentials are deferred by choice — it is a credential-management change, not an architectural one |
| R9 | Moving slot creation out of review-approval breaks any request approved under the old flow | Keep the legacy branch: if the request's farm already has a slot, do nothing; only new approvals skip minting. Test both paths (`test_m10_08_cutover.py`) |
| R10 | A manual assignment with no installer available for a province strands the request | Assignment is optional at approval; the request stays APPROVED and visible in a support "unassigned" queue. No auto-timeout |
| R11 | The 15-minute installer token expires mid-install (slow site, phone asleep) | Client re-opens the session silently on 401; test that path explicitly. A field engineer blocked at a farm gate is a support call, not a bug report |
| R12 | Removing farm-manager approval leaves "gateway live with no working config" guarded only by `applied == desired` | Enforce the condition in the reconciler, never in the UI. Add a test that a slot cannot reach `PROVISIONED` with `applied < desired` |
| R13 | A platform-staff installer could read a whole org if an endpoint forgets the `farm_id` filter | `scoped_farms()` returns "unnarrowed" for zero scope rows (`accounts/permissions.py:76`). Every installer endpoint must filter by the claim's `farm_id`; add a test per endpoint |
| R14 | A photographed QR label lets someone claim a unit before the real installer does | Claim requires an assigned installer, is audited, is reversible (re-issue a code), and is a denial-of-service rather than a breach — the impostor still cannot authenticate to the broker. Do not weaken this by making the claim code reusable |
| R15 | Sensors carry two identifiers (`pack_id` node id and `HardwareRegistry.hardware_id`) that can disagree | Installer enters only `pack_id`; `hardware_id` stays optional. Confirm with the hardware team whether they are the same string, and drop one from the UI if so |
| R16 | Firmware must not be assumed to have an LED, a buzzer, or any indicator | Binding is designed with zero device behaviour (AD-18). Do not reintroduce a "press the button" or "watch the light" step |
| R17 | **`scripts/check_rls_parity.py` reports pre-existing drift** (9 tables missing from `initdb/02_rls.sql`, 11 from the migration set: `daily_reports`, `farm_events`, `farm_event_assignments`, `farm_event_targets`, `notification_deliveries`, `report_medication_usages`, `role_panel_permissions`, `support_tickets`, `vet_medication_catalog`, `alert_subscriptions`, `user_panel_overrides`) | **Not caused by Phase 0** — those tables came from the v0.9/v1.x reports work and were never enrolled. The Phase 0 trio is consistent in all three sources. Do not "fix" this by deleting entries; enrol the missing tables in a dedicated change, with the owner's sign-off, because it turns RLS on for tables that currently run without it |
| R19 | **The CI test DB is reused across local Docker runs** (`--reuse-db` in pytest.ini + a persistent postgres container), so a model change shows up as `UndefinedColumn` on many tests rather than a clear schema error. The first symptom after Phase 1 was 105 "failures" that were one stale schema | Run local Docker gates with `pytest --create-db` after any model or migration change. The CI runner's postgres starts empty, so this is a LOCAL-ONLY trap. |
| R20 | The `test` CI service mounts `./initdb` and `./monitoring` but read `emqx/emqx.conf` from the image, where it does not exist | Fixed: `./emqx:/emqx:ro` added to the `test` service volumes, matching the existing repo-root-mount convention. |
| R18 | `apps/mqtt_auth/publisher.py` imports `paho` at call time, not module load | Intentional: a worker with no broker configured must import cleanly. Keep it that way — a module-level import makes the whole Celery worker fail to boot when paho is absent |

### Unverified assumptions (do not treat as fact)

- That EMQX 5.5 accepts a multi-JOIN postgres authn query with a `LEFT JOIN`
  and an `OR` branch. **Must be proven on the rehearsal stack before Phase 1
  ships.** If it is rejected, the fallback is two separate authenticator
  entries or a view that flattens both tables.
- That the firmware team can ship a build subscribing to `devices/{id}/desired`
  **and** `devices/{id}/command` in the current cycle. This is the only
  remaining external dependency and it gates Phase 4 cutover, not Phases 0–2.
- That retained-message volume (~1–4 KB × fleet) is acceptable in the broker.
  Owner has approved the concept; the number is an estimate.

### Things that will look wrong but are not

- Two hashing systems (Argon2/PBKDF2 for users, bcrypt for devices). Correct
  and deliberate — EMQX can only verify bcrypt.
- `PASSWORD_HASHERS` listing `BCryptSHA256PasswordHasher` last. Irrelevant to
  devices; do not "fix".
- The status topic being subscribed but producing nothing. That is F10, a real
  bug, not a design choice.
- `command_logs` existing with no writers. Intentional Phase-2 actuator
  ledger, not an oversight.

---

## Next Agent Instructions

1. **Read** `docs/mqtt-provisioning-redesign-v1.md` §1, §3, §5, §7, §9.
2. **Confirm with the owner** that Phases 0–1 are approved. **No questions
   remain open** — every clarification is answered (see "Answers received" and
   design doc §12).
   Four decisions need explicit sign-off because they are hard to reverse:
   installers as platform staff (AD-14), removing farm-manager approval
   (AD-17), the new `installation_jobs` table (AD-16), and binding by QR claim
   code rather than a password (AD-18).
3. **Before writing any code**, verify these three facts still hold (the tree
   may have moved since 2026-10-18):
   - `grep -rn "import paho" backend/` → only the subscriber.
   - `grep -rn "add_dev" .` → only `.md` files.
   - `grep -rn "GatewaySlot.objects.create" backend/apps/` → only
     `provisioning/api.py`.
4. **Read design doc §1.4 before designing anything.** The command channel is
   already specified (v0.5 E1–E6, Blueprint §11.2) — implement it, do not
   re-invent it. Conversely, the org-less inventory and desired-state model
   have no prior art and are genuinely new.
5. **Start with Phase 0.1–0.3** (the three migrations). They are additive and
   reversible. Run the full suite after each.
6. **Then Phase 0.4–0.5** (grants + publisher principal), which need a live
   Postgres to verify.
7. **Do not touch** `apps/telemetry/`, `apps/alerts/`, `apps/monitoring/`,
   `apps/compat/`, or `command_logs`. The telemetry pipeline must not be
   perturbed by this work.
8. **Do not deploy** `emqx.conf` changes without `emqx check config` and a
   rehearsal-stack boot.
9. **Update this file** after every phase and after every architectural
   decision. Add to "Confirmed Findings"; never delete a valid earlier one.

### Working conventions in this repo

- Tests: `backend/tests/test_*.py`, pytest, `--nomigrations` by default;
  `@pytest.mark.use_migrations` only for RLS assertions. Coverage gate 80%.
- Every non-obvious code change carries an EN + FA `why` comment naming the
  finding/bug id (project convention — see `apps/provisioning/services.py`).
- Migrations must have a reversible `reverse_code`; `initdb/*.sql` changes need
  a migration twin.
- New visible documentation is authored in English (project policy).
- Deploy: images are pre-built by tag; editing `backend/` on the VPS changes
  nothing. See `DEPLOYMENT.md` §8.

---

## Last Verified State

**Date:** 2026-10-18 (Phases 0–1 implemented)
**Verification performed:** Phase 0/1 modules locally (sqlite) + the FULL suite
in Docker (`docker-compose.ci.yml` `test` service: TimescaleDB + Redis + the
real pytest command), and `makemigrations --check`.

| `pytest` (full suite, Docker: TimescaleDB + Redis) | **797 passed, 8 skipped, 0 failed** |
| full CI gate (coverage + module gate) | **GREEN** — total coverage **88.96%** (floor 80%); `check_module_coverage.py`: "every request path meets its floor" |
| full CI gate (coverage + module gate) | **GREEN** — total coverage **88.83%** (floor 80%); the request-path floor is met (only 4 non-blocking WARNs on the backfill queue). `check_module_coverage.py`: "GREEN — every request path meets its floor". |
| `docker-compose.ci.yml` | torn down with `down --volumes` afterwards; the `morghdari-test:ci` image is kept for reuse |
| `makemigrations --check --dry-run` | `No changes detected` |
| `pytest tests/test_m10_0*.py` (local sqlite) | 69 passed, 1 skipped (Postgres-gated) |
| `scripts/check_rls_parity.py` | the 3 new org-scoped tables are consistent in all three sources; **pre-existing drift remains** (R17) |

| Check | Result |
|---|---|
| `pytest tests/test_m10_00_foundations.py` | **27 passed** |
| `makemigrations --check --dry-run` | `No changes detected` |
| `pytest tests/test_m5_01_transition.py` | 3 passed |
| `pytest tests/test_m5_06_07_08_enforcement.py` | 22 passed |
| `scripts/check_rls_parity.py` | The 3 new tables are consistent across database.md / initdb / 0006. **Pre-existing drift remains** — see "Known Issues" |
| `grep -rn "import paho" backend/` | now 2 hits: the subscriber **and** `apps/mqtt_auth/publisher.py` (expected — Phase 0 built the publisher) |
| `grep -rn "add_dev"` across repo | still `.md` files only — `add_dev` stays unimplemented on purpose (superseded by the desired-state document) |

**The full suite passes in Docker.** The three "local failures" recorded during
Phase 0 were all environmental and are now resolved by running the suite the way
CI runs it (TimescaleDB + Redis available):

| Module | Local sqlite | Docker (Postgres + Redis) |
|---|---|---|
| `test_m5_02_03_provisioning_api.py` | 2 failed | **passes** |
| `test_m5_04_05_trust_boundary.py` | 13 errors (no Redis) | **passes** |
| `test_m2_05_tenant_middleware.py` | 4 `test_pg_*` failed | **passes** |
| `test_m6_06_ws.py::test_inbound_message_refreshes_ttl` | — | failed once on the first Docker run (TimeoutError) and has passed on every subsequent run, including two full-suite reruns → **flaky under load**, not a regression. Worth watching; if it fires twice in a row on the CI runner, investigate the WS channel-layer timeout. |

- **Task 2.16 — role widening for installer sessions.** **IMPLEMENTED 2026-10-20.**
  `saloon_create` and `position_create` (both in `apps/farms/api_follow.py`) were
  manager-only, which meant an installer could not describe the farm they were
  standing in. Both now accept an **installer session scoped to that farm**:
  `_installer_session_farm()` requires the `installer_session` claim, a matching
  `org_id`, and a `farm_id` claim equal to the target farm. A session for farm A
  cannot write into farm B, and a session with no `farm_id` gets no access rather
  than all access. `support_session` deliberately does NOT open the writer
  (AD-14). Tests: `test_m11_05_role_widening.py` (7), including both negatives.

- **Task 2.9 — device photos (AD-20).** **IMPLEMENTED 2026-10-20.**
  `POST /provisioning/installations/{id}/gateway/photo/` and
  `POST …/{id}/sensors/{slot_id}/photo/` — a photo per gateway AND per sensor,
  which is what AD-20 asks for ("each sensor is an independent entity").
  Both go through one helper that reuses `ops_services.photo_error` and its
  §2 O6 limits, so the saloon-photo path and the device-photo path cannot drift
  apart. Exactly-one-target is enforced as a 400 rather than a 500 from the
  model's CHECK constraint, and `is_primary` clears the previous primary for the
  SAME target so a gallery keeps one cover image instead of "last one wins".
  Tests: 9 in `test_m11_03_installation_jobs.py`. Contract → v1.14.0.

- **Task 2.10 — the sensor mapping (AD-23).** **IMPLEMENTED 2026-10-20.**
  `POST /provisioning/installations/{job_id}/sensors/` binds a physical
  `pack_id` (the 8-hex LoRa node id) to a `saloon_sensor_slots` row, and bumps
  `desired_config_version` so the device treats the next retained document as
  NEW. This is the join that makes the writer in 2.8 actually useful: the sensor
  reaches the device through `sensors[].pack_id` with its saloon, position,
  label and `metrics` — asserted by driving `build_desired_state` for real, not
  by checking the mapping row.
  A slot in another farm is a 404 (an installer cannot map onto a building they
  are not working on), one physical pack can only be in one place (409), and
  `pack_id` is strictly validated — a typo'd node id is the most expensive class
  of bug to trace, because it surfaces as a "stalled" gateway on a farm that
  looks installed. Tests: 6 new in `test_m11_03_installation_jobs.py`.
  Contract → v1.13.0.

- **Task 2.8 — the missing saloon sensor-slot writer (D5 / X3).** **IMPLEMENTED 2026-10-20.**
  `POST /saloons/{saloon_id}/sensor-slots/` in `apps/farms/api.py`. Contradiction
  X3 in the design doc is now resolved: the Actor Use-Case Map named three
  writers for `saloon_sensor_slots` and none existed, because
  `provisioning_sync` only *validates* slots — a saloon could never be given
  one through the API. A slot is a mounting plan, not the sensor itself, so
  there is deliberately no `pack_id` here; that binding is slice f.
  Tests: `test_m11_04_sensor_slot_writer.py` (9), including the assertion that
  the queryset `provisioning_sync` validates against now finds slots created
  this way — the writer and the validator agree. Contract → v1.12.0.

### The first-device rehearsal (2026-10-20)

Owner context: **no devices are installed anywhere; the project is still in
development.** That reframes the remaining work — and it means Phase 4's
deprecation scaffolding protects nobody.

What it exposed: the full install loop had only ever run against the CI broker
(built-in-DB authn), and the production profile had only ever been asked "will
you let this unit connect". **A first real device does both at once, and that
combination was untested.**

`scripts/first_device_rehearsal.py` now runs the SAME flow function the live
test uses (imported, not copied) against the production broker profile:
register → request → approve → assign → bind → device applies → sensor →
submit → PROVISIONED, authenticated by Postgres the whole way. **It passes, and
re-runs cleanly.**

It is a script, not a pytest module, for a specific reason: pytest isolates the
test database, so the rows land in `test_<name>` while the broker queries the
real one — every device is refused "not authorised" for a reason that has
nothing to do with the code. Forcing them together means fighting
create/flush/teardown against a schema owned by real migrations.

Four rig gaps found on the way, each of which looked like a code bug:
- the publisher verifies TLS and needs `MQTT_TLS_CA`; production sets it, the
  rig did not → `CERTIFICATE_VERIFY_FAILED`, which looks like a broken device;
- the cert's SAN does not cover the internal name `emqx`, so production also
  sets `MQTT_TLS_INSECURE=1` (documented accepted risk);
- `REDIS_URL` must carry no db path (settings append `/0`);
- the flow needs a real Redis for `transition()`'s lock.

Also made the flow **re-runnable**: it commits (the broker must see the rows),
so its fixed usernames and `pack_id` collided on a second run. All are now
run-unique — a real property, since the CI e2e uses a fresh database and never
showed the problem.

### Phase 3 — the command channel (2026-10-20)

Closes finding D9: a fully-specified subsystem (v0.5 tasks E1–E6 + Blueprint
§11.2) that vanished by hand-off to a v0.6 that never picked it up.

* **3.1** the subscriber now subscribes AND routes
  `+/farms/+/gateways/+/commands/response`. Routing happens BEFORE the telemetry
  parser, because that parser rejects anything not ending in `/telemetry` — the
  same trap that silently dead-lettered the status lane (D1).
* **3.2** `ROTATE_CREDENTIAL` commits only when the device PRESENTS the new
  password. A bare SUCCESS is not proof it can authenticate with it.
* **3.3** `DIAGNOSTICS_REQUEST` keeps the device's report in the audit ledger.
* **3.4** `sweep_command_expiry` on the 60 s beat: a SENT command the device
  never answered becomes EXPIRED instead of sitting in flight forever.
* **3.5** already fixed (the D1 status-lane routing).

Two rules the tests pin: a late response NEVER resurrects a FAILED/EXPIRED row
(it records `late …` in `last_error`), and an unknown `command_uuid` is IGNORED
rather than treated as a fault — devices replay their outbox after a reconnect.

### The production broker profile — VERIFIED (2026-10-20)

The last unproven link is closed. `docker-compose.broker-prod.yml` reproduces
production faithfully — Postgres built from the real `initdb/` scripts, Django
migrations, then EMQX mounting `emqx/emqx.conf` **verbatim** on TLS 8883 — and
`test_m12_00_prod_broker_authn_live.py` proves seven properties against it:

1. an **unbound factory unit authenticates** — the `device_units` LEFT JOIN
   branch that AD-1 depends on, which no other test could exercise (the CI
   broker would pass it for the wrong reason);
2. a bound PROVISIONED gateway still authenticates (the OR branch is intact);
3. a RETIRED unit is refused;
4. an unknown username is refused;
5. a wrong password is refused (bcrypt runs against the Postgres hash);
6. an inactive credential is refused;
7. an unbound unit still cannot reach a tenant topic — proven with a **witness
   credential that is genuinely able to subscribe**, so a denied publish and an
   unobservable witness cannot be confused.

**A production blocker was found and fixed.** `accounts/0005_system_user.py`
called `user.set_unusable_password()` on a model obtained from
`apps.get_model()`, which returns a HISTORICAL model — fields only, none of
`AbstractUser`'s methods. Every real `migrate` would have died with
`AttributeError`. It had never executed anywhere because the suite runs
`--nomigrations`; the rehearsal was the first real migration. Fixed with
`make_password(None)`.

**Two rig bugs, both instructive:**
- `EMQX` does not substitute `${VAR}` inside `emqx.conf`; the `*_SERVER` env
  overrides are what actually wire the pool. Production documents this in
  `docker-compose.prod.yml` and I had copied only part of the block.
- `initdb/01_roles.sql` grants SELECT only `IF to_regclass(...) IS NOT NULL`.
  At container-init time the tables do not exist, so EVERY grant is skipped and
  `emqx_auth` ends up with no SELECT — the broker then reports "Not authorized"
  for valid credentials. Production re-runs the grants post-migrate
  (`qa_bringup.sh` step 3); the rig now does too.

### Phase 2 status: COMPLETE (2026-10-20)

All seventeen tasks are implemented: 2.1–2.17. The path runs end to end —
request → approve (farm + jobs, nothing minted) → assign → session → bind by
claim code → sensor slot → sensor mapping → submit → PROVISIONED with no
manager → the four-state view. The live loop proves it against a real broker.

**Two pre-existing infrastructure defects were found by the new tests:**

- **`legacy_slug_map` never existed on a cold Postgres test DB.** The model is
  `managed = False`, so `--nomigrations` syncdb skips it, and its table lives
  only in `compat/0001`'s RunSQL. `mint_saloon_slug()` runs on EVERY saloon
  creation, so creating a saloon through the API had never worked under CI — it
  simply had never been attempted. The role-widening tests (2.16) were the first
  to do it. Fixed in `conftest._materialize_compat_schema()`, deliberately NOT
  gated on the `use_migrations` marker, because this is core topology rather
  than an optional reports feature.
- The same RLS masking class as before: any org-less-platform surface must be
  exercised at least once against real Postgres.

### Live end-to-end — the FULL installation loop (2026-10-19)

`test_m10_03_bootstrap_lane_live.py::test_full_installation_loop_end_to_end_tx`
drives the whole product against a real EMQX 5.5 broker, in the order a farm is
actually installed:

1. the factory registers a unit with **no organisation** (Phase 1)
2. a manager requests a farm **the org does not have yet** (slice a)
3. support approves — farm materialised, 2 jobs created, **nothing minted**
4. support assigns the job; the installer picker confirms the assignment (slice b)
5. the installer **binds by claim code** while the unit is offline (slice c);
   the retained configuration reaches the real broker
6. the device connects, receives, validates and **APPLIES** it — an independent
   implementation of the firmware contract, speaking the real wire protocol
7. the installer submits; the reconciler activates with **no manager** (slice d)
8. the config-state view reports PROVISIONED with `applied == desired` (slice e)

Only the HTTP layer is in-process (`APIRequestFactory` + `force_authenticate`,
the same boundary the unit tests use). The Django subscriber is not running in
that container, so its lane is modelled by calling `reconcile_reported`
directly — stated in the test's docstring rather than hidden.

**Bugs this live run found that no unit test could:**
- **`_org_ids_for` / `_find_job` / `provisioning_config_state` read
  `organizations` UNscoped.** Every org-scoped table is FORCE RLS, so an
  org-less staff caller — no GUC set — saw ZERO orgs and the queue showed an
  empty world. Invisible under sqlite, where RLS does not exist; the same
  masking class as D15 (review approvals on the VPS). Fixed by discovering over
  `admin_alias()` at all three sites.
- The config-state `?state=` filter covers the four UNCONVERGED states only, so
  an activated slot is read from the unfiltered view (`state == PROVISIONED`).
  The test now asserts exactly that instead of a wrong expectation.

### Live end-to-end — the bootstrap lane (2026-10-19)

The one thing Phase 0/1 could not prove with unit tests is whether a REAL broker
agrees. That gap is now closed:

| Asset | Purpose |
|---|---|
| `docs/hardware-firmware-requirements-v1.md` | The document to send the firmware team. Eight sections: three identities, what the factory does, the five device behaviours (§3), the complete-not-delta config contract (§4), what must NOT be assumed (§5 — no LED/buzzer/button), edge cases, and the five-line summary. |
| `scripts/firmware_sim.py` | An INDEPENDENT implementation of §3/§4 of that contract. **Must never import `apps/` or `morghdari/`** — the moment it reuses the backend's own `topics.py` or `desired_state.py`, a bug in our grammar stops being caught. It is also the reference implementation we hand the firmware team. |
| `backend/tests/test_m10_03_bootstrap_lane_live.py` | Three live scenarios, skipped unless `MQTT_BOOTSTRAP_HOST` is set, so the normal `test` service just reports three more skips. |
| `docker-compose.ci.yml` → `e2e-bootstrap` | New service: same test image, plus the `./scripts` mount, plus `MQTT_HOST` for the backend's own publisher (without it the retained-config publish silently degrades to "not configured" and the round trip has nothing to round-trip). |

**What it proves against a real EMQX 5.5 under `no_match= deny`:**

1. a factory unit with **no org and no farm** authenticates;
2. an unbound unit is **DENIED** the tenant telemetry topic — proven by a snooper
   already subscribed to `+/farms/+/gateways/+/telemetry` receiving nothing;
3. the unit **can** publish to its own `devices/{id}/reported` lane (a real
   PUBACK, not a rule row that looks right);
4. the backend's retained config reaches the independent device, which validates
   `slot_id`, enforces the monotonic `config_version` guard, applies the whole
   document, and its report feeds the real reconciler → `applied_config_version`
   lands in Postgres;
5. after retire the ACL rows are gone, the credential is deactivated, and the
   broker refuses the next CONNECT.

**Two profile notes to keep straight.** The CI broker profile is built-in-DB
authn, so the credential is seeded through the management API exactly as
`emqx_provision.py` does for the dev stack, and the CI listener is plaintext
1883 (the test honours `MQTT_BOOTSTRAP_TLS` for both profiles). The
postgres-backed authn query in `emqx/emqx.conf` is the production/rehearsal
profile — covered by `test_m10_01`'s Postgres-gated SQL test, which executes the
exact query against the real schema.

### Remaining before Phase 1 can ship

| Item | Why | Status |
|---|---|---|
| EMQX accepts the new `emqx.conf` | The only broker-wide edit in the whole plan. | **DONE 2026-10-18** — `docker run emqx:5.5 emqx foreground` with the modified file boots clean: "EMQX 5.5.1 is running now!", both listeners up, zero config errors. (`emqx check config` reports "Node is not running" on 5.5.1 booting a fresh container, which is a known wrapper quirk — the boot log is the stronger check anyway.) The SQL itself is separately exercised by the Postgres-gated test `test_authn_query_accepts_registered_unit_against_real_sql`, which runs the exact query against the real schema. |
| A live EMQX end-to-end of the bootstrap lane | Proves a real unit can CONNECT with a factory credential and subscribe to `devices/{id}/desired` under the new ACL rows. | **DONE 2026-10-19** — `test_m10_03_bootstrap_lane_live.py`, 3/3 green against live EMQX 5.5 with `no_match=deny`, covering CONNECT, SUBACK, the tenant-publish denial, the retained-config round trip through the real reconciler, and post-retire refusal. |
| `live-verify` service in Docker | It ships with the CI compose but was not exercised. | **NOT RUN** — separate from the new e2e-bootstrap service. Worth running once before the next release, but it covers M5-07/M5-08 behaviour that predates this work and is unchanged by it. |

**Resource caps** (added to `docker-compose.ci.yml` at the owner's request, so a
laptop run cannot freeze the machine): postgres 512m/1.0 cpu, redis 192m/0.5,
emqx 512m/1.0, test 1.5g/2.5, migrate-gate 768m/1.0, live-verify 768m/1.0,
image-scan 1g/1.0. Total steady state ≈ 2.3 GB, which fits a 4 GB Docker VM.
