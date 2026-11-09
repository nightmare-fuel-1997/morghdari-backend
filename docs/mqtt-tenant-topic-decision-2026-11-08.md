# MQTT tenant identity: org/farm in the topic vs. a database lookup

**Date:** 2026-11-08
**Status:** assessment, no code changed
**Scope:** gateway telemetry topic scheme (T1 `{org}/farms/{farm}/gateways/{slot}/telemetry`)
**Question:** should the topic carry `org_id` and `farm_id`, or only the device id with the
tenant resolved server-side?

This document reports what the code actually does, then compares the two designs. It is not
an argument for the current implementation. Where an earlier verbal justification was wrong,
it says so.

---

## 0. Verdict first

1. **Performance is not a differentiator.** The byte difference is ~48 B/message and the
   lookup is a single primary-key hit. At any plausible rate both designs are fine. Any
   argument based on speed is unsupported by this codebase and should be dropped.
2. **Under the stated constraint — gateways never move farms — the two designs are close to
   equivalent in correctness.** The current design's main theoretical advantage (attribution
   fixed at publish time) only matters if a slot's `org_id` can change after telemetry is
   published. If it cannot, that advantage is worth little.
3. **The real difference is where the org comes from and what privilege that costs.**
   Today: the broker's ACL attests it, and the hot path needs no privileged read.
   Alternative: the database decides it, and the hot path needs a cross-tenant read.
4. **The current implementation does NOT cross-check topic org against the database.**
   The claim that "two independent sources agree on the org" is false — only the slot id is
   cross-checked. Details in §3.1.
5. **Recommendation:** keep the current scheme unless you are willing to do the migration
   properly. If the team wants device-only topics, implement **Option C** (narrow
   `SECURITY DEFINER` resolver + immutability constraint), not Option B. If the scheme is
   ever going to change, change it now, before firmware ships — after that it is a fleet
   reflash.

---

## 1. Current implementation, factually

### 1.1 Data model

| Table | Tenant column | Nullable | Notes |
|---|---|---|---|
| `organizations` | — | — | root of tenancy |
| `farms` | `org_id` | **YES** | `farms/models.py:15` — "farm row may precede org assignment completing" |
| `saloons` | `org_id` | no | denormalized for RLS (`farms/models.py:44`) |
| `gateway_slots` | `org_id` | no | `devices/models.py:61`, `on_delete=RESTRICT` |
| `gateway_slots` | `farm_id` | **YES** | `devices/models.py:62` — a slot can exist before a farm is assigned |
| `device_units` | — | — | **org-less by design**, no RLS (`devices/models.py:309-363`) |
| `mqtt_device_credentials` | — | — | org-less, read by the broker |
| `mqtt_acl_rules` | — | — | org-less, read by the broker |

`DeviceUnit.unit_id` **is** the eventual `gateway_slots.slot_id` (AD-2,
`devices/models.py:321-323`). Binding never reflashes the device and never reissues its
credential — it creates one server-side link.

**What is immutable today:**

- `DeviceUnit.unit_id` / `hardware_id` — factory-assigned primary identity.
- Nothing else is immutable at the database level. `gateway_slots.org_id` and `farm_id` are
  ordinary foreign keys with no trigger, no check, and no append-only history table. They
  *can* be updated by SQL or a shell script.

**What is mutable through the API:** essentially nothing, because no endpoint writes them.
`install_job_assign` (`installations.py:368-440`) reassigns the **installer**, not the
gateway — `reassigned_after_bind` refers to the job assignee.

**Can a registered device exist without org/farm?** Yes — that is the entire point of
`DeviceUnit`. It has no `org_id` column at all.

**When are org/farm assigned?** At bind, in `_bind_job_to_unit` (`installations.py:515-580`):
`GatewaySlot.objects.get_or_create(slot_id=unit.unit_id, defaults={org, farm, ...})`, then
ACL expansion, then a `desired_config_version` bump, then a queued `PROVISION` command.

**Can a device be rebound or transferred?** Not through any implemented endpoint. The retire
endpoint returns 409 on a bound unit with the message "unbind it first"
(`api_units.py:271-274`), but **no unbind route exists** — `accounts/urls.py` exposes only
`units/{unit_id}/retire/`. So the documented escape hatch is currently unimplemented. An
operator with DB access could still move a slot between orgs by hand.

**What happens to historical telemetry after reassignment?** Telemetry rows carry their own
`org_id` (RLS hypertables), so existing rows stay with the old org. Nothing rewrites history.
A queued message in flight would be attributed according to whichever design is in use — see
§4.

### 1.2 MQTT authentication

`emqx/emqx.conf:92-117`. PostgreSQL backend, bcrypt, dedicated `emqx_auth` role:

```sql
SELECT cred.password_hash AS password_hash
FROM mqtt_device_credentials cred
LEFT JOIN gateway_slots gs ON gs.slot_id = cred.device_id
LEFT JOIN device_units   du ON du.unit_id  = cred.device_id
WHERE cred.username = ${username}
  AND cred.is_active = true
  AND (gs.provisioning_status IN ('PENDING_INSTALLATION','PENDING_APPROVAL','PROVISIONED')
       OR (gs.slot_id IS NULL AND du.state IN ('REGISTERED','BOUND')))
```

- **Username** = `mqtt_device_credentials.username` = `unit_id` = `slot_id`
  (`api_units.py`, registration path).
- **Client ID** is **not** validated against the device id — any unique string is accepted.
  Verified live during the plaintext test window.
- Password is a bcrypt `$2b$` hash; `ensure_gateway_user` is effectively a no-op in
  production because authn is Postgres-only.
- The provisioning gate lives in the `WHERE` clause: a unit in a PENDING state can still
  connect, but a REJECTED/SUSPENDED slot fails authn at the broker.

### 1.3 MQTT authorization

`emqx/emqx.conf:123-143`:

```hocon
sources = [{ type = postgresql
  query = "SELECT permission, action, topic FROM mqtt_acl_rules WHERE username = ${username}" }]
no_match    = deny
deny_action = ignore
```

There are **two ACL writers** and a bound gateway holds the **union**:

| Writer | Rows | Content |
|---|---|---|
| `bootstrap_acl_rule_items()` (`mqtt_auth/services.py:292`) | 4 | `devices/{slot}/{desired,command}` subscribe; `devices/{slot}/{reported,command/response}` publish |
| `gateway_acl_rule_items()` (`provisioning/api.py:84-117`) | 9 | T1–T9 tenant topics: publish telemetry/status/commands-response/ota-response/diagnostics-response; subscribe commands/clock-sync/ota/diagnostics-request |

All rules are **literal topics keyed on the username** — no wildcards. `action` is
`publish`, `subscribe` or `all`.

`deny_action = ignore` means a denied publish is dropped silently with the connection left
open. Measured during the 2026-11-03 test: foreign SUBACK `128`; spoof publish
`delivered=False`; connection stayed up.

### 1.4 RLS and database roles

`initdb/01_roles.sql:24-38` declares five roles:

| Role | Purpose | Privilege |
|---|---|---|
| `morghdari_owner` | migrations/DDL | owns nothing at runtime |
| `morghdari_app` | Django, Celery, subscriber | DML only, subject to RLS |
| `morghdari_admin` | platform staff / bootstrap | **BYPASSRLS — and in production the cluster superuser** |
| `emqx_auth` | broker authn/authz | SELECT on 4 tables only |
| `morghdari_exporter` | metrics | `pg_monitor`, no data DML |

`initdb/02_rls.sql:22-94` applies `ENABLE` + `FORCE ROW LEVEL SECURITY` and one policy to
every org-scoped table:

```sql
USING      (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
WITH CHECK (org_id = current_setting('app.current_org_id', true)::UUID)
```

Unset GUC → `NULL` comparison → **zero rows**. Fail-closed by construction.
`tenant_context()` (`apps/tenants/context.py:30-40`) sets the GUC with
`set_config(..., true)` — transaction-scoped, dies at COMMIT, cannot leak across a pooled
connection.

**Important correction to a common assumption:** the admin alias is not a narrow
BYPASSRLS role. `docker-compose.prod.yml:139-142` sets `POSTGRES_USER: ${POSTGRES_ADMIN_USER}`
with `POSTGRES_ADMIN_USER=morghdari_admin` (`.env.vps.example:24`), which makes that role the
**bootstrap superuser** of the cluster. The `CREATE ROLE ... BYPASSRLS` in `01_roles.sql` is
skipped because the role already exists. Consequences: the alias can read and write every
org's rows, run DDL, and reach every database in the cluster. Its *code paths* are narrow
(SELECT only); its *capability* is not.

There are already two precedents for narrow, role-scoped RLS exemptions:

- `emqx_auth_read` — `FOR SELECT TO emqx_auth USING (true)` on `gateway_slots`
  (`02_rls.sql:108-117`), so the broker can read slots without an org GUC.
- `org_members_self_lookup` and `lifecycle_templates_global_read` — same shape,
  `02_rls.sql:127-156`.

### 1.5 Telemetry pipeline

`morghdari/subscriber/pipeline.py:95-163`:

1. **Gate 1** — `parse_topic()` extracts `(org_id, farm_id, gateway_slot)` from the topic
   (`subscriber/topics.py:17-32`). No database read.
2. **Gate 2** — `identity_fault()` requires the payload's mirrored `gateway_slot_id` to equal
   the topic's gateway segment, else DLQ `TOPIC_PAYLOAD_IDENTITY_MISMATCH`.
3. **Gate 3** — `schema_version` must equal `2.0`.
4. **Gate 4** — `device_ts` required.
5. **Gate 5** — `resolve_pack_slot(redis_client, org_id, hardware_id, pack_id, db_fetch=...)`
   — cache-first sensor resolution, **scoped by the org from the topic**.
6. **Gate 5.5** — provisioning status gate (PENDING held, REJECTED/SUSPENDED → DLQ).
7. **Gate 6** — dedup key `(org_id, slot_id, device_ts)` in Redis.

The subscriber's own subscription is a wildcard across all orgs
(`main.py:50`, `$share/telemetry_workers/+/farms/+/gateways/+/telemetry`). Tenant isolation
is therefore enforced on the **device** side by ACL, not on the subscriber side — the
subscriber is trusted infrastructure by design.

### 1.6 Firmware and the desired-state document

`provisioning/desired_state.py:114-139` publishes a complete, retained document containing:

```json
{ "org_id": "...", "farm_id": 12,
  "topics": { "telemetry": "{org}/farms/{farm}/gateways/{slot}/telemetry",
              "status": "...", "commands": "...", "commands_response": "...",
              "desired": "devices/{slot}/desired", "reported": "...", "command": "..." } }
```

`desired_state.py:12-15` (AD-7): the firmware **derives the same strings itself and compares
them**; a mismatch means a mis-addressed document and the device refuses it. So the topic
grammar lives in firmware, and the document also carries the values.

**Consequence:** the firmware already stores `org_id` and `farm_id`, and already knows the
topic grammar. "Simpler firmware" is therefore a weak argument for either design — and a
topic-scheme change is a **firmware release**, not just a backend change.

---

## 2. Comparison

| Dimension | A. Tenant-prefixed topic (current) | B. Device-only topic + lookup |
|---|---|---|
| Topic example | `{org}/farms/12/gateways/GW-7F3A/telemetry` (~81 chars) | `devices/GW-7F3A/telemetry` (~33 chars) |
| Bytes per publish | baseline | **~48 B less** |
| Org source | broker ACL (literal topic) | database row |
| Cross-tenant read on hot path | none | **required** (`admin_alias` = superuser) |
| RLS context opened before first query | yes | no — lookup must precede it |
| Attribution fixed at publish time | yes (protocol-level) | no (mapping at delivery time) |
| Device can publish into another org's namespace | no (ACL) | n/a — no org in topic; still cannot use another slot's topic |
| Org/farm consistency sources | **two** (ACL row + `gateway_slots`) | one |
| Firmware changes to adopt | — | topic grammar + constants |
| Farm move requires topic/ACL rewrite | yes | no |
| Subscriber filter change | — | all T1/T2 filters + `parse_topic` |
| Test/doc surface change | — | routes inventory, topic maps, 4 test files, protocol doc |

### Performance, with numbers

Assumptions stated openly; no measurements exist in the repo.

- **Topic bytes.** org UUID = 36 chars; `farms/`, `gateways/`, `telemetry` ≈ 45 chars.
  Delta ≈ 48 B per publish. At 1,000 gateways reporting once a minute (16.7 msg/s) that is
  **~0.8 KB/s** — under 0.01 % of a 1 Gbit link. At 100 msg/s it is ~4.8 KB/s. Negligible.
- **Lookup cost.** Primary-key `SELECT org_id, farm_id FROM gateway_slots WHERE slot_id = $1`
  is an index-only hit, order 0.05–0.3 ms warm. At 16.7 msg/s that is ~0.3 % of one core.
  It can be cached in Redis next to `resolve_pack_slot`, which is already cache-first.
- **Tenant context.** One `set_config` per transaction — one extra round trip either way;
  B adds a query, not necessarily a new transaction.
- **Admin alias pool.** One extra pooled connection per subscriber/celery process. On a
  2-CPU VPS with a bounded `max_connections`, that is a real but small operational cost.
- **Broker ACL evaluation.** EMQX caches authorization results by default (TTL ~1 min); the
  config does not override this. Topic length does not affect the cost materially.

**Conclusion:** neither design wins on performance. Do not decide on this axis.

---

## 3. Security and correctness

### 3.1 What is enforced vs. assumed — current design

| Property | Status |
|---|---|
| A device cannot publish into another org's topic | **Enforced** — literal ACL rows, `no_match=deny` |
| A device cannot publish as another device | **Enforced** — ACL keyed on username = slot id |
| The payload's `gateway_slot_id` matches the topic | **Enforced** — Gate 2, DLQ |
| The topic's `org_id` matches `gateway_slots.org_id` | **NOT CHECKED** — no code compares them |
| The topic's `farm_id` matches `gateway_slots.farm_id` | **NOT CHECKED** |
| Telemetry is written under the correct tenant | **Assumed**, resting entirely on ACL correctness |

That fourth row is the important one. The pipeline trusts the topic's org because the ACL
pinned it. There is no second opinion at ingest. If an ACL row were ever written with the
wrong org (the D21 class of defect — `_restore_db_acl_rules` once rebuilt the wrong rule set,
`test_2026_10_24_live_defects.py:808`), telemetry would be attributed to that org and RLS
would accept it, because the context is derived from the same wrong value. **The current
design has two places where the tenant is recorded (ACL row and `gateway_slots`), and nothing
reconciles them.**

### 3.2 What is enforced vs. assumed — alternative B

| Property | Status |
|---|---|
| A device cannot publish as another device | **Enforced** — ACL pins `devices/{slot}/...` |
| The slot resolves to the right org | **Enforced by the lookup** — single source |
| The lookup is trustworthy | depends on a superuser-privileged connection |
| Attribution survives a reassignment | **No** — mapping is read at delivery time |

B has one source of truth instead of two, which is genuinely better on consistency. It pays
for that with privilege: every telemetry message would require a read that the app role
cannot perform, on a connection that can also `DROP TABLE`.

Note the alias is **already present** on the subscriber containers
(`docker-compose.prod.yml:259, 307`) and is already used by `_discover_slot`
(`tasks.py:43-52`) for the low-volume `reported` lane. B's incremental risk is not "a new
credential appears"; it is "the highest-volume path in the system now depends on it
permanently".

### 3.3 Impersonation

A compromised device holding a valid credential can:

- publish anything it likes **within its own literal topics** — bounded by Gates 2, 3, 5;
- **not** publish into another device's or another org's namespace (ACL);
- **not** subscribe to other devices' lanes (ACL).

The real impersonation risk is operational, not architectural: two physical devices sharing
one username (exactly what the test unit `GW-TEST-B784019A` does) are indistinguishable at the
broker. That is a credential-hygiene issue, identical under both designs.

---

## 4. Message lifecycle scenarios

| Scenario | A (tenant topic) | B (lookup) | Wrong-tenant risk |
|---|---|---|---|
| Telemetry queued for an offline subscriber | topic carries org; attribution fixed | lookup at consume time | B only, if the mapping changed while queued |
| Device offline, reconnects later | no change | no change | none |
| Retained `desired` delivered after rebind | retained doc is on `devices/{slot}/desired` in **both** designs; rebind bumps `desired_config_version` (AD-4) so the device treats it as new. **Nothing clears the retained doc on retire** — ghost config exists in both | same | none from the topic scheme; the ghost-config gap is orthogonal and unfixed |
| Device provisioned while offline | works — retained `desired` on the device lane | same | none |
| Backend publishes new desired while device offline | retained, delivered on reconnect | same | none |
| Telemetry arrives with a stale/incorrect org mapping | server cannot detect it (§3.1) | lookup returns the *current* org, so "stale" is not expressible — but a wrong DB row is undetectable | **both** can misattribute; A silently, B silently |

Under the "gateways never move" constraint, rows 1 and 6 are the only places where the
designs differ, and both require a mapping change that no current code performs. That is why
the correctness difference is small **given the constraint** — and why it stops being small
the moment a transfer or ops-level SQL edit happens.

---

## 5. Firmware and provisioning

- **Where does firmware get org/farm?** From the retained `desired` document
  (`desired_state.py:118-119`), and it also derives the topic strings itself (AD-7).
- **Must it store them?** It already does — `org_id` and `farm_id` are in the document it
  must persist to survive a flash wipe.
- **How are they updated?** A new `desired_config_version` is published; the device applies
  and reports `applied_config_version`. Same under both designs.
- **Partial provisioning failure?** Bind is one transaction (`installations.py:515-580`);
  a failure rolls back the slot, the ACL expansion and the version bump together. The
  `PROVISION` command is queued, and `sweep_config_convergence` retries while
  `applied < desired`. The device lane is unchanged either way.
- **Topic constants per design:** A needs 4 tenant strings + 3 device strings (7, all
  delivered in the document). B needs 1–3 device strings. Both are delivered by the
  document, so the difference is a few bytes of JSON, not firmware logic.
- **Reliability cost of storing org/farm in firmware:** essentially zero, because the
  document already carries them and the firmware must persist the document anyway.

**Real cost of changing the scheme:** it is a firmware release, not a backend deploy. If the
fleet is not yet deployed, that cost is near zero *today* and grows permanently after.

---

## 6. Assumptions that cannot be verified from this repository

1. That firmware actually derives topic strings rather than merely consuming them. The
   docstring says it derives and compares (AD-7); the firmware source is not in this repo.
2. That EMQX's authorization cache is enabled with defaults. `emqx.conf` does not configure
   `authorization.cache`, so defaults apply; not verified on the running broker.
3. Real telemetry rate per gateway and fleet size.
4. That no operator or runbook updates `gateway_slots.org_id`/`farm_id` by hand. No code path
   does; SQL could.
5. That `morghdari_admin` is intended to be the superuser long-term. Compose makes it so;
   `01_roles.sql` describes it as merely `BYPASSRLS`.
6. Whether EMQX 5.5's `password_based:postgresql` backend can return `client_attrs` (which
   would allow the ACL to template the tenant from the credential rather than the topic).
   Plausible, unverified — see Option D.

## 7. Missing information for a definitive decision

- Firmware's topic-derivation code and its release cycle.
- Expected devices and messages/second at 12 months.
- A written business rule on gateway transfer, and whether an unbind/reassign flow is planned
  (the retire endpoint already references one that does not exist).
- Whether `DATABASE_ADMIN_URL` should remain a superuser in the target security posture.
- Whether any tenant is expected to own more than one farm per gateway zone, which would make
  `farm_id` in the topic genuinely load-bearing for routing.

---

## 8. Options, including a third one

### Option A — keep tenant-prefixed topics (status quo)

**Pros:** org is attested by the broker, not derived by the app; hot path needs no privileged
read; attribution fixed at publish; no migration; matches the documented "org prefix is
unforgeable evidence" rule for ingested telemetry.

**Cons:** ~48 B/message; two sources for the tenant with no reconciliation; farm move rewrites
topic + ACL; firmware carries the grammar.

**Cost to adopt:** zero.

### Option B — device-only topics + `admin_alias` lookup

**Pros:** short topics; single source of truth for the tenant; firmware needs one string; farm
move does not change the topic.

**Cons:** every telemetry message depends on a **superuser** connection; RLS context cannot
open before the lookup; attribution is delivery-time; migration touches subscriber filters,
`parse_topic`, ACL generation, four test files, the protocol doc and firmware.

**Cost:** medium; the security cost is the deciding factor, not the engineering cost.

### Option C — device-only topics + narrow resolver + immutability (recommended if changing)

Two small additions make B safe:

1. **Narrow resolver instead of the admin alias.** A `SECURITY DEFINER` function
   `resolve_slot_tenant(p_slot text) RETURNS TABLE(org_id uuid, farm_id int)` owned by a role
   with `SELECT` on `gateway_slots`, `EXECUTE` granted to `morghdari_app`. The caller gains no
   cross-tenant read capability — only two columns for one slot id. This follows the existing
   `emqx_auth_read` precedent (`02_rls.sql:108-117`): minimal, SELECT-only, single-table,
   single-role.
2. **Immutability constraint.** A trigger (or `BEFORE UPDATE` check) rejecting changes to
   `gateway_slots.org_id`/`farm_id` once `provisioning_status = 'PROVISIONED'`, plus an
   append-only `slot_tenant_history` row for the rare authorised transfer. This restores
   attribution stability — the one property A gets for free.

**Result:** short topics, simple firmware, no superuser on the hot path, stable attribution,
tenant isolation preserved. Cost: one function, one trigger, one cache key.

### Option D — tenant identity in the credential (unverified sketch)

If EMQX 5.5 can surface `client_attrs` from the Postgres authn query, the ACL could template
`${client_attrs.org_id}` and the topic could stay flat while the broker still attests the
tenant. This would be the best of both worlds. **Not verified** — the current authn query
returns only `password_hash`, and this needs a broker capability test before it can be
considered.

---

## 9. Recommendation

**Keep Option A for now.** Not because it is faster — it is not — and not because it is
simpler — it is not. Keep it because:

- it is already implemented and tested, and the fleet has not shipped, so the decision can be
  deferred without a migration penalty *today*;
- its one durable advantage is real: the org on the hot path is attested by the broker's ACL
  rather than derived by application code, and the privileged connection stays off the
  telemetry path.

**But fix the two things that make A weaker than it looks:**

1. Add a reconciliation check at ingest: compare the topic's `org_id`/`farm_id` against
   `gateway_slots` (cache the result per slot, as `resolve_pack_slot` already does). Today
   there is no such check, and the ACL is a single point of failure for tenant attribution.
2. Stop calling the admin alias a narrow privilege. It is the cluster superuser. Either
   demote it to a true `BYPASSRLS` role with table grants, or replace its two discovery call
   sites with a `SECURITY DEFINER` resolver.

**If the team wants device-only topics, choose C, not B**, and do it before firmware ships.
B's simplicity is real but it is bought with a superuser read on every telemetry message and
with attribution that follows the database rather than the message.

**The one-line answer for the hardware team:** *"We can index it — we already do for the
pre-bind lane and for saloon. The question is which component attests the tenant. In the topic
it is the broker; in a lookup it is our own database read, which needs a privileged connection
and can change under a message that is already in flight. Saloon is looked up because it
changes; org is pinned because it is ownership."*

---

## 10. Sources

- `backend/apps/devices/models.py:59-94, 309-363`
- `backend/apps/farms/models.py:12-49`
- `backend/apps/provisioning/api.py:84-117`
- `backend/apps/provisioning/installations.py:515-580`
- `backend/apps/provisioning/tasks.py:43-52, 60-130, 263-289`
- `backend/apps/provisioning/topics.py:1-19`
- `backend/apps/provisioning/desired_state.py:114-139`
- `backend/apps/mqtt_auth/services.py:292-306`
- `backend/apps/mqtt_auth/models.py:36-53`
- `backend/apps/devices/api_units.py:253-280, 409-415`
- `backend/apps/tenants/context.py:30-40, 62-82`
- `backend/config/settings/base.py:63-87`
- `backend/morghdari/subscriber/pipeline.py:95-163`
- `backend/morghdari/subscriber/topics.py:1-42`
- `backend/morghdari/subscriber/main.py:50-51, 352-391`
- `emqx/emqx.conf:92-117, 123-143, 165`
- `initdb/01_roles.sql:24-38`, `initdb/02_rls.sql:22-94, 108-156`
- `docker-compose.prod.yml:139-142, 259, 307`
- `.env.vps.example:24`
- `backend/tests/test_2026_10_24_live_defects.py:808`
