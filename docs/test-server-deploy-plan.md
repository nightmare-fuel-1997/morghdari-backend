# Test-server deployment — assessment and plan (2026-10-22)

**Verdict: yes, deploy the latest version to the existing test server — but not by
running `deploy.sh` as-is.** The code is ready; four things about the previous
deployment's wiring no longer hold, and one of them silently breaks the
simulator. The plan below fixes them in an order where each step is verifiable
before the next.

**Status: in progress.** §1–§4 were written before touching the server. §5 is the
server inspection, now done — it corrected two of my own assumptions (see the
note on RLS there), and it lowered the risk rather than raising it: the existing
simulator gateways are `PROVISIONED` and their credentials still authenticate,
so they survive the deploy unchanged. §6 is the live runbook; items are marked
done as they complete.

---

## 5. Server inspection — done 2026-10-22

Host `82.115.17.252` (`srv1661423174`, Ubuntu 24.04, Docker 29.1.3). Checkout at
`/opt/morghdari`, branch `main`, **working tree clean**, deployed tag `vps-02`,
image built 7 days ago. All ten containers up and healthy; the simulator is
publishing continuously.

| Check | Result |
|---|---|
| Compose files in use | `docker-compose.prod.yml` + `docker-compose.vps.yml` (+ `sim.yml`) |
| Deployed commit | `8031059` — **6 commits behind**, clean fast-forward confirmed |
| Broker config | `emqx/emqx.conf` mounted verbatim, plus `*_SERVER`/`*_DATABASE`/credential env overrides |
| Authn enforced? | **Yes, proven empirically** — see below |
| Simulated gateways | `GW-SIM-01`, `GW-SIM-F3`, `GW-SIM-F4` — all **PROVISIONED** (farms 2/3/4) |
| Their credentials | active, bcrypt `$2b$`, 9 ACL rows each — **they still authenticate** |
| `.sim.env` | 371 bytes, mode `600`, owned by `ubuntu`; 3 entries, fields `username`/`password`/`farm_id` |
| Telemetry | **278,792 rows** in `saloon_telemetry`, newest ~2.5 h old — data is landing |
| Subscribers | `subscriptions=2` — confirms finding F4 |
| Other slot | `GW-TEST-01` `PENDING_INSTALLATION` (a leftover test unit) |

**Authn probe (decisive, run from a one-off container against the live broker):**

| Attempt | CONNACK |
|---|---|
| unknown username | `5` not authorised |
| known user, wrong password | `4` bad credentials |
| `GW-SIM-F3` + its real password | **`0` accepted** |

**A correction to my own earlier reading, worth recording.** My first query of
`gateway_slots` returned *0 rows*, which would have meant the simulator could
not authenticate at all. That was an artifact of **RLS**: the table is
row-security-scoped, and the Django application role sees nothing without a
tenant context, while the `emqx_auth` role is exempt (that exemption is
`tenants/0003`). Querying as the admin role showed the six real rows. The
empirical probe settled it. The lesson is the one this project keeps teaching:
**a query that returns empty is not the same as a table that is empty** —
especially with RLS in play.

**What this changes:** the deploy is lower-risk than §3 assumed. The three
simulated gateways authenticate under the new query too, because they are
`PROVISIONED` and the new authn query's first branch is
`gs.provisioning_status IN ('PENDING_INSTALLATION','PENDING_APPROVAL','PROVISIONED')`.
Their credentials, ACL rows and pack mappings are untouched by the migration.

**Backup taken** (before any change): `/root/backups/predeploy-20261022/` —
7.2 MB `pg_dump -Fc`, plus copies of `.env` and `.sim.env` (mode `600`), plus the
deployed SHA. 35 GB free on disk.

---

## 1. What the previous deployment actually was

Reconstructed from the repo, not from the server:

| Piece | Where | Evidence |
|---|---|---|
| Stack | `docker-compose.prod.yml` + `docker-compose.vps.yml` overlay, `--env-file .env` | the two-file convention in `DEPLOYMENT.md` §8; `vps.yml` overrides `daphne`, `celery-worker`, `celery-beat`, `postgres` only |
| Simulator | `docker-compose.sim.yml` → `simulator/sim.py` | `sim.yml` runs `python /sim/sim.py`; mounts `./.sim.env` at `/sim/creds.json` |
| Broker | `emqx/emqx.conf` mounted verbatim, TLS 8883 | `docker-compose.prod.yml` §emqx |
| Sim credentials | `./.sim.env`, git-ignored | `.gitignore` line 35; `sim.yml` line 34 |

`simulator/sim.py` is 88 lines and does exactly one thing: reads
`/sim/creds.json` (a map of `slot → {password, farm_id}`), opens one TLS MQTT
connection per gateway, and publishes a schema-2.0 telemetry payload every
`SIM_INTERVAL_SEC`. Two sensor packs per gateway (`P-A`, `P-B`). It does **not**
publish status/LWT, does **not** subscribe to commands, and does **not** apply
configuration.

Its docstring records the dependency that no longer holds: credentials were
"minted by the review-approve flow, never hand-made."

---

## 2. What changed since (measured, not estimated)

`git diff --stat 0677a44..HEAD` — 30 files in `backend/apps`, **+4308 / −46**.

**Untouched (this is the good news for the frontend):** `apps/telemetry` has
**zero** changes. `apps/alerts` is +14 additive lines in
`services/redis_keys.py` (LWT keys for the status lane). The frontend's
data-reading surface — farms, saloons, dashboards, telemetry queries, alerts —
is contract-identical.

**Changed in ways that matter:**

| Area | Change |
|---|---|
| Provisioning | New flow: factory unit → request → review (creates farm + jobs, **mints nothing**) → assign → bind by claim code → apply → submit → `PROVISIONED` |
| New tables | `device_units`, `installation_jobs`, `device_photos`, `platform_audit_log` + RLS |
| Broker authn | Now a LEFT JOIN: an **unbound** factory unit may authenticate (Phase 1) |
| Subscriber | Now holds **five** lanes (was three) |
| Simulator path | `emqx_provision.py` still seeds the broker's **built-in database**, which `emqx.conf` does not use |

---

## 3. Five findings that change the plan

### F1 — The simulator's credential source no longer exists (blocking)

The review-approve response used to return `gateway_slot_id`, `username` and a
one-time `password` (see `0677a44:apps/provisioning/api.py`). Those fields fed
`.sim.env`. The new response returns `farm_id` and `jobs_created` and **no
credential at all** — deliberately, because approval no longer mints anything.

**Consequence:** a fresh deploy cannot produce a `.sim.env` the way it used to.
The old simulator keeps working only for gateways whose credentials already
exist in the database.

### F2 — `emqx_provision.py` cannot create simulator users on this broker

It writes to EMQX's built-in database through the management API
(`AUTHN_CHAIN_ID = "password_based:built_in_database"`). `emqx.conf`
authenticates **only** against Postgres. Only `docker-compose.dev.yml` overrides
to `built_in_database` — and that is dev-only by the config's own note.

**Consequence:** the old "provision sim users" step is a no-op here. New
simulated gateways must get their identity through the backend, the same way a
real device does.

### F3 — Sim telemetry is invisible to the frontend unless the slot is PROVISIONED

Gate 5.5 in `morghdari/subscriber/pipeline.py`: telemetry from a
`PENDING_APPROVAL` gateway is held in the Redis stream
`telemetry:pending_approval` — **never written to the database**. The frontend
would show an empty farm while the simulator publishes happily.

**Consequence:** a simulated gateway must be driven all the way to `PROVISIONED`
before its data is worth anything. That is a product rule, not a bug, but it
changes what "the simulator works" means.

### F4 — The subscriber's new lanes will be denied until its ACL is re-seeded

Yesterday's fix gave the subscriber five subscriptions
(`test_m13_01_device_lanes.py`). `seed_subscriber_credentials` now writes five
ACL rows — but the server's `subscriber-01`/`subscriber-02` rows were seeded
with the old two. `mqtt_acl_rules` is read live by the broker, so the two new
lanes will be denied with a `SUBACK DENIED` critical log and an endless
resubscribe loop.

**Consequence:** re-run `seed_subscriber_credentials` for both subscribers as
part of the deploy. One command each, idempotent.

### F5 — Migrations and grants have never run against a database with data

Four migrations are new (`accounts/0005`, `devices/0002`,
`provisioning/0002`, `tenants/0006`), plus RLS policies. `accounts/0005` was
itself broken until yesterday (`e375892`) — it called
`set_unusable_password()` on a historical model, which would have failed every
real `migrate`. It has only ever run on fresh databases.

Also load-bearing, from the September QA bring-up: `initdb/01_roles.sql` grants
are written `IF to_regclass(...) IS NOT NULL`, so at container-init time **every
grant is skipped**. Production re-runs `01_roles.sql` **and** `02_rls.sql`
*after* migrating (`qa_bringup.sh` steps 3–4). Skipping that leaves `emqx_auth`
with no SELECT, and the broker then answers "not authorized" for perfectly
valid credentials — which looks exactly like a broken device.

**Consequence:** rehearse the migration on a **copy** of the database first.

---

## 4. Recommendation, and the trade-off I rejected

**Recommended:** deploy to the existing server, in the order below, after
rehearsing the migration on a database copy.

**Rejected — a parallel clean environment.** Cleaner, and it protects the test
data the frontend is already built against. But it costs a second broker, a
second database, a second TLS cert and a second set of environment, and the
frontend's base URL would change — which is the one thing the task says to
preserve. The old data is exactly what makes this deployment worth doing.

**Rejected — reusing `emqx_provision.py` to make simulator users.** It only
works by switching the broker to the dev profile (`built_in_database` +
`no_match=allow`), which would silently drop authorization enforcement on a
stack that is meant to rehearse production. Not worth it for convenience.

---

## 5. Deployment plan

Ordered so that each step is verifiable before the next one is taken.

1. **Inspect, change nothing.** Record: git SHA and tag currently deployed,
   `docker compose ps`, which compose files are in use, `.env` keys (names
   only), the broker's authn profile, and whether `.sim.env` exists and what
   shape it has. Confirm the working tree is clean and no server-side edits
   would be lost.
2. **Back up.** `pg_dump` of the database plus a copy of `.env` and `.sim.env`,
   stored outside the repo. *(server)*
3. **Rehearse the migration on a copy.** Restore the dump into a scratch
   database, run `migrate`, then re-run `initdb/01_roles.sql` and
   `initdb/02_rls.sql` as the QA bring-up does. Only proceed if this is clean —
   it is the step most likely to surprise us.
4. **Fetch the code.** `git fetch` + checkout `e375892` (current `main`), never
   a blind `git pull`. Verify the SHA.
5. **Build the image on the server.** There is no CI workflow in this repo —
   nothing builds or pushes to `ghcr.io` automatically — and the local Docker
   engine here cannot start, so building on the server is the path. Tag it
   explicitly and record the tag.
6. **Migrate for real**, then re-apply the two grant scripts (step 3's second
   half).
7. **Re-seed the subscriber ACLs.** `seed_subscriber_credentials` for
   `subscriber-01` and `subscriber-02` with the existing usernames and
   passwords. This fixes F4.
8. **Recreate the app services** with the project's own convention
   (`--no-build`, the recorded tag), keeping the existing env. Health-gate
   before declaring success.
9. **Re-seed the publisher credential** (`seed_publisher_credentials`) so the
   desired-state publisher can authenticate — it is the worker that delivers
   configuration to devices.

---

## 6. Simulator plan

**Preserve:** `simulator/sim.py` stays the publisher. It already satisfies the
requirements — multiple gateways across multiple farms from one `creds.json`,
one connection per gateway, schema-2.0 payloads on the real MQTT path, each
message carrying `gateway_slot_id` + `farm_id` and per-reading `pack_id`, and
sustained repeats at `SIM_INTERVAL_SEC`.

**Replace the credential source.** A new script drives the real install flow
per simulated gateway — factory-register a unit, create a gateway request,
review it, assign the job, bind by claim code, let the device apply its config,
submit — and writes the resulting credentials into `.sim.env` in exactly the
shape `sim.py` already reads. No simulator-only shortcuts: the simulated
gateway goes through the same pipeline a real one does, which is what makes the
test meaningful.

**Keep the old farms alive.** For gateways already in `.sim.env` whose slots are
`PROVISIONED`, the existing credentials keep authenticating unchanged — the
migration is additive and does not touch `mqtt_device_credentials` or
`mqtt_acl_rules`. Those gateways need no re-provisioning.

**One optional improvement, flagged not assumed:** publishing status/LWT so the
four-state view and LOS watchdog mean something for simulated farms. Cheap, but
it is a behaviour change to a file the frontend team may rely on, so it is a
question for you rather than something I do silently.

**One hard requirement to check on the server:** each pack (`P-A`, `P-B`) must
still resolve through `(org, hardware_id, pack_id)` to a `saloon_sensor_slots`
row, or Gate 5 sends every reading to the DLQ and the farm stays empty. The old
mappings live in the database and should survive, but it is a one-query check.

---

## 7. Frontend compatibility

**Verified by inspection, not by running the frontend:**

- Farms, saloons, telemetry, alerts, dashboards: **unchanged**. `apps/telemetry`
  has no diff at all.
- Same base URL, same TLS, same auth behaviour.
- **One breaking change, and it is small:** the review-approve response no
  longer carries `gateway_slot_id` / `username` / `password`; it returns
  `farm_id` and `jobs_created`. Any frontend code that read those fields — most
  likely an installer-credentials screen — must be updated. Nothing else in the
  contract moved.

---

## 8. Credentials

**The existing ones can be kept.** `.sim.env`'s usernames and passwords remain
valid for any gateway whose slot is `PROVISIONED`, because the migration does
not touch the credential or ACL tables. The subscriber and publisher service
accounts likewise keep their existing usernames; only their ACL rows need
re-seeding. No secret needs to be committed, logged, or hardcoded — `.sim.env`
stays git-ignored and is read from the server's own environment.

---

## 9. Deployment — COMPLETE and verified (2026-10-22/23)

| # | Step | State |
|---|---|---|
| 1 | Inspect, change nothing | **done** — see §5 |
| 2 | Back up | **done** — `/root/backups/predeploy-20261022/` (7.2 MB dump, `.env`, `.sim.env`, deployed SHA) |
| 3 | Fetch + checkout | **done** — `8031059` → `e375892`, clean fast-forward |
| 4 | Build the image | **done** — `ghcr.io/morghdari/backend:vps-03`, 501 MB, built on the server |
| 5 | Rehearse migration on a copy | **done** — all five migrations applied cleanly to 278,798 real rows |
| 6 | Migrate for real + grants + RLS | **done** — as `morghdari_admin` (house path), then `01_roles.sql` + `02_rls.sql` |
| 7 | Re-seed subscriber ACLs | **done** — both subscribers now hold **5** subscriptions (were 2) |
| 8 | Seed the publisher credential | **done** — `django-backend`, bcrypt, 6 publish-only ACL rows; password generated on the server, stored only in the git-ignored `.env` |
| 9 | Recreate services | **done** — all six backend services on `vps-03`, health-gated |

**Verified after the deploy:**

| Check | Result |
|---|---|
| `https://…/api/v1/health/` | `200`, database + Redis `ok` |
| Containers | daphne / celery-worker / celery-beat / subscriber-01 / subscriber-02 / simulator / postgres all healthy; emqx, nginx, redis untouched |
| Images actually running | all six backend services on `backend:vps-03` (`25a3935bee60`); postgres on `postgres-walg:vps-03` (same image ID as `vps-02`, retagged) |
| Subscribers | `subscriptions=5` each — the two new device lanes are live |
| Simulator | still publishing `1 msg x 3 gateways`; all 3 clients connected |
| Data | 282,036 telemetry rows, newest seconds old — no data lost |
| Authn, bound gateway | `GW-SIM-F3` → authenticates |
| Authn, **unbound** unit | a `REGISTERED` unit with no slot and no org → **authenticates** (the new LEFT JOIN branch) |

**Three things worth recording:**

1. **`MORGHDARI_TAG` is shared by two images.** `docker-compose.prod.yml` uses
   `${MORGHDARI_TAG}` for both `backend` and `postgres-walg`. Bumping the tag
   therefore needs `postgres-walg:<tag>` to exist too, or `up -d` fails on
   postgres. Fixed by retagging the running `vps-02` image (identical ID) as
   `vps-03` — the postgres image is unchanged by this deploy.
2. **nginx keeps a stale upstream after the app containers are recreated.**
   `up -d` left nginx returning `502` while daphne was healthy — a reload fixes
   it. `scripts/deploy.sh` already does this; a manual `up -d` does not.
3. **`.env` had no publisher credential at all.** `PUBLISHER_USERNAME` /
   `PUBLISHER_PASSWORD` were absent, so the new celery worker (which is now the
   MQTT publisher) would have had no way to authenticate. Generated on the
   server and seeded; nothing was printed or committed.

**Also worth knowing:** the live migration script ran twice — a network error
re-executed it, so the first run applied the migrations and lost its output to
the command timeout, and the second correctly reported "no migrations to
apply". The end state was verified independently rather than assumed.

**Known cosmetic issue:** the health payload still reports `version: 0.8-DR`.
That string comes from a settings constant, not the image, so it does not track
the tag. The authoritative evidence of the running build is the image ID above.

---
## 10. The simulator — a digital twin, and the bug it found

The old simulator was a telemetry generator: it connected, published schema-2.0
readings, and never subscribed, never applied a configuration, never answered a
command and never published status. It could not exercise the parts of the system
that matter, and it could not fail the way hardware fails.

`simulator/twin.py` is now a **DigitalTwin** — one simulated physical device
implementing `docs/hardware-firmware-requirements-v1.md` from scratch, with no
imports from `apps/` or `morghdari/`, so a bug in our shared grammar still gets
caught. `simulator/sim.py` is the fleet that runs one twin per gateway.

### What it does, and what that proved live

| Step | What the twin does | Verified on the server |
|---|---|---|
| Connect | TLS, MQTT 3.1.1, `username = unit_id` | all 3 twins connected |
| Subscribe | `devices/{id}/desired` + `/command` | granted, after the ACL fix below |
| Receive config | the retained document, published by the backend's own publisher | 3 retained documents present |
| Apply | validate → monotonic guard → apply → report `APPLIED` | `applied_v=1`, `state=CONFIGURED` |
| Record | `reconcile_reported` updates the slot | `applied = desired = 1` on all three |
| Telemetry | schema 2.0, per-pack readings on the tenant lane | 6 sensor slots receiving rows continuously |
| Identify | `gateway_slot_id`, `farm_id`, `pack_id` on every message | per-slot row counts distinct and current |

Telemetry sample from a live twin — realistic, complete, in range:

```json
{"co": 3.23, "lux": 289.13, "nh3": 6.46, "temp": 23.34,
 "water_l": 0.89, "humidity": 58.14, "power_gen": 0.0, "power_mains": 217.68}
```

### The bug the twin found

The first deploy of the twin showed all three gateways connecting and then
sitting at `applied_v=0` forever. The subscriber's log said why:

```
reconcile_reported: unknown slot GW-SIM-01
```

`reconcile_reported()` discovers the slot over `admin_alias()`, and
`admin_alias()` falls back to `"default"` when no `DATABASE_ADMIN_URL` is
configured. Every tenant table is FORCE ROW LEVEL SECURITY, so a context-free
read as the app role returns **zero rows by design**. The subscriber only ever
had `DATABASE_URL`.

**Consequence: the entire config-apply lane was dead in production.** Every
device reported `APPLIED`, the backend logged "unknown slot", and nothing else
looked wrong — so `applied_config_version` could never leave 0 and the
submit → PROVISIONED check could never pass. The same silence that hid the
status lane (D1) and the command lane (D9), a third time.

Fixed in `404c219`: both subscribers now carry `DATABASE_ADMIN_URL`, the same
posture daphne and the celery worker already use — discovery over the admin
alias, per-tenant reads and writes still inside `tenant_context`.

### Two more things the twin exposed

1. **The legacy gateways had no device-lane ACL at all.** The old approval flow
   granted nine tenant rules and nothing on `devices/...`. With the broker's
   `deny_action = ignore`, a denied SUBSCRIBE is *silent*: the client connects,
   authenticates, and simply never receives anything. Granted with the backend's
   own `bootstrap_acl_rule_items()`.
2. **The legacy gateways had never been issued a configuration.**
   `desired_config_version` was 0 and no retained document existed — the old
   flow minted a slot, credential and ACL and stopped. Issued through the
   backend's own `publish_desired_state_for()`, exactly as bind does.

### What it models beyond the happy path

* **Fault injection**, all off by default so a clean run stays clean:
  `SIM_FAULT_DROP` (skip windows), `SIM_FAULT_DELAY` + `SIM_FAULT_DELAY_SEC`
  (late sends), `SIM_FAULT_INVALID` (readings outside physical range),
  `SIM_FAULT_RESTART` (power cycles). Documented in `docker-compose.sim.yml`.
* **Reconnection with backoff**, and `applied_version` surviving a reboot — a
  real device re-applies nothing on boot, which is what makes the server's
  monotonic guard meaningful.
* **Status with a Last Will**, so an unclean death reads as OFFLINE. Only once
  bound: an unbound device has no status topic, and publishing to one the ACL
  denies is exactly the kind of noise a twin should not make.
* **The command channel in three separate events** (ACK → act → SUCCESS), an
  expired command *answered* rather than ignored, an unknown action FAILED with
  a reason, and rotation that proves the new password and defers the reconnect.

### Known limitations — documented, not approximated

* **Sensor physics is a diurnal model, not a shed.** Values are plausible and in
  range; they are not a thermal simulation.
* **No TLS client certificate.** The fleet authenticates with username/password
  (Phase 1 posture), so neither does the twin.
* **The pack ids are `P-A`/`P-B`.** The current sensor-map endpoint requires an
  8-hex LoRa node id, so these legacy mappings could not be re-created through
  the API — they predate that rule and were left as they are.
* **New gateways ARE auto-provisioned** — see §12. What is not automated is the
  *scheduling* of new installations (a human or a cron still decides when the
  fleet grows); the installation itself is fully driven.

---

## 11. Integration testing — results

Run against the live deployment on 2026-10-23. Every claim below is an observed
result, not an inference from configuration.

### Service health

| Check | Result |
|---|---|
| All ten containers | up; daphne / celery-worker / celery-beat / subscriber-01 / subscriber-02 / postgres healthy |
| `https://…/api/v1/health/` | `200`, database `ok` (26 ms), Redis `ok` (3 ms) |
| Six backend services on `vps-03` | confirmed by image ID, not by tag |

### Simulator

| Requirement | Result |
|---|---|
| Multiple farms concurrently | 3 gateways across farms 2, 3 and 4, all `CONFIGURED`, 16 windows sent each |
| Sustained repeated transfers | 6 sensor slots receiving a window every 60 s, continuously |
| Which device produced which point | `gateway_slot_id` + `farm_id` in the topic, `pack_id` per reading; per-slot row counts are distinct and current |
| Intermittent connectivity | `SIM_FAULT_DROP` ran a fleet that skipped windows; no crash, no stall |
| Delayed transmission | `SIM_FAULT_DELAY` + `SIM_FAULT_DELAY_SEC` implemented and exercised |
| Invalid readings | `SIM_FAULT_INVALID` produced out-of-range values (temp 135 °C, humidity 285 %) |
| Device restarts | `SIM_FAULT_RESTART` power-cycles a twin; `applied_version` survives |
| Authentication failure | an unknown username is refused with CONNACK 5, a wrong password with CONNACK 4 |

### Invalid data — what the backend actually does

Probed the pipeline directly with four broken messages. All four were
dead-lettered into `telemetry:dead_letter` with the correct reason:

| Sent | Reason recorded |
|---|---|
| `schema_version: "1.0"` | `UNSUPPORTED_SCHEMA_VERSION` |
| unmapped `pack_id` | `UNKNOWN_SLOT` |
| `gateway_slot_id` ≠ topic slot | `TOPIC_PAYLOAD_IDENTITY_MISMATCH` |
| `{not json` | `bad_json` |

**Worth knowing:** out-of-*range* values are **ingested, not rejected**. The
pipeline validates structure (schema, identity, timestamp, pack resolution,
provisioning status); plausibility is an alerting concern, not an ingest one.
That is a design property, not a defect — but it means `SIM_FAULT_INVALID`
exercises the alert path, not the DLQ.

### End-to-end data flow

Twin → broker → subscriber → Redis stream → worker → Postgres, verified at each
hop: the twins report `APPLIED`, `applied_config_version` reaches 1 on all three
slots, and rows land per sensor slot within the same second they are published.

### Frontend compatibility

Logged in through the real flow (`POST /api/v1/auth/login/` → JWT) and read the
data the frontend reads:

* `GET /api/v1/farms/` — the farm list with locations, saloons, gateway status
  and alert counts, in the contract shape.
* `GET /api/v1/saloons/2/telemetry/?since=…&slot_id=…` — readings from the twins
  **minutes old**, with `server_received_ts`, `device_ts`, `is_buffered`,
  `buffered_delay_mins`, `slot_id` and the full `metrics` object.

**No API contract changed.** `apps/telemetry` has no diff since the deployed
baseline, and the one response-shape change in the whole release — the
review-approve endpoint losing `username`/`password`/`gateway_slot_id` — is in
the provisioning surface, not in anything the frontend reads for data.

### Regression

`pytest` on the changed and adjacent modules: **65 passed** (twin, fleet, device
lanes, EMQX provisioning, subscriber boot).

### Notes and hygiene

* The DLQ stream is `telemetry:dead_letter`. (`telemetry:dlq` does not exist —
  checking the wrong name reads as an empty queue, which is how this was nearly
  missed.)
* The temporary staff account created for the API check was deleted afterwards,
  along with its membership and sessions.

---

## 12. Auto-provisioning — new gateways install themselves

`simulator/provision.py` creates a NEW gateway the way a farm is really
installed, driving the backend's own API views:

| # | Step | Who | What it proves |
|---|---|---|---|
| 1 | register a unit, **org-less** | factory (platform staff) | the factory branch works; returns a password + claim code |
| 2 | request a farm the org lacks | manager | `gateway_requests` insert, org-scoped |
| 3 | approve | reviewer | farm + job materialise, **nothing is minted** |
| 4 | assign the job | support | installer sees it |
| 5 | **bind by claim code** | installer | the gateway slot is created here |
| 6 | saloon + sensor slot + pack mapping | installer | telemetry will resolve |
| 7 | submit | installer | → `PROVISIONED` |

Run it against the live server:

```
SIM_PROVISION_COUNT=1 docker compose -p morghdari --env-file .env \
  -f docker-compose.prod.yml -f docker-compose.vps.yml -f docker-compose.sim.yml \
  run --rm --no-deps provisioner
```

It writes `/sim-state/provisioned.json`, which the simulator merges and reloads
every 30 s — **a new gateway joins a running fleet with no restart**. Verified:
three gateways installed this way, all reaching `applied = desired = 3`, all
publishing. An existing twin is never restarted by a reload.

### Two findings from making this work

**1. Two packs on one sensor slot silently lose half their data.**
The ingest dedup key is `(slot, device_ts)` with **no pack component**, so the
second reading of a window that resolves to the same slot is indistinguishable
from a redelivery and is dropped. The provisioner originally created one slot
for both packs; the legacy gateways are 1:1, which is the only reason they never
hit it. `provision.py` now creates one slot per pack, and the three existing
auto gateways were migrated to match — all 12 sensor slots now receive every
window.

**2. A pack-mapping change does not take effect for up to 24 hours.**
`morghdari/subscriber/resolver.py` caches `(hardware_id, pack_id) → slot` in
Redis for 24 h. Migrating the mappings above changed the database and changed
nothing on the wire, until the stale keys were deleted:

```
docker exec morghdari-redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning \
  --scan --pattern 'slot:map:*' | xargs -r docker exec morghdari-redis \
  redis-cli -a "$REDIS_PASSWORD" --no-auth-warning DEL
```

Worth knowing before anyone re-maps a pack in production and concludes the
change did not work.

### Also learned the hard way

* **The platform gate wants `is_platform_staff`, not Django's `is_staff`.**
  A caller created with `is_staff` alone gets `403 platform staff required`.
* **Calling a view directly skips `TenantOrgMiddleware`**, so
  `app.current_org_id` is never set and every tenant table refuses the write.
  The harness sets the same GUC inside a transaction, exactly as the middleware
  does — and *only* for org-scoped callers, matching the middleware's
  platform-staff skip.
* **The org, job, saloon and position are written over the admin alias**, with
  explicit ids rather than object references (a reference across connections
  trips the database router's relation check).
* **The unit tests cannot catch any of this.** The test database carries no
  `initdb` RLS policies, so a bare view call works there. Only the live server
  exercises the real posture — which is the argument for the twin in the first
  place.

---

## 13. Verification round — what checking (instead of assuming) turned up

The instruction was to keep going until everything was certain. Three things
were not, and all three were invisible from the outside.

### 1. The command channel could never send a command

Every twin reported `cmds=0`, which I had read as "nothing has been sent to
them". The truth was stronger: nothing **could** be. `publish_command` and
`reconcile_command_response` both existed and were both tested, but no
application code ever called the publisher — the only callers were tests. The
channel was fully specified, fully reconciled, and completely dead.

`rotate_credential` had the same hole one level up: it staged a new password and
returned it once, but never told the device, so `rotation_ack` could only succeed
if a human carried the password to the gateway by hand.

Fixed by `issue_command()` (creates the row, enqueues the publish `on_commit`)
and `POST /gateways/{slot}/commands/`. Rotation now issues the command that
carries the password — the command **is** the delivery. §12's table of the three
command events now actually happens.

### 2. Three test-harness tables were gated behind a marker they do not need

`DailyReport`, `DailyReportExtra` and `MetricDailyAggregate` are all
`managed = False` with a `RESTRICT` FK to Saloon, so Django's delete collector
queries them whenever a saloon is deleted — marker or no marker. Under
`--nomigrations` they exist only as `RunSQL`, so every farm/saloon delete test
failed on a cold DB with `relation "..." does not exist` for a table it never
mentions.

`legacy_slug_map` was fixed exactly this way in September. The same bug was then
repeated twice. All three now sit beside it in `_materialize_compat_schema`.

### 3. How the suite is actually run

The dev box cannot run this suite: it has no local Redis, so 14 tests in
`test_followthrough_core.py` fail on connection refused before any assertion
runs. That looked like a code problem and was not.

The suite now runs **on the server**, in a throwaway container against a
throwaway database (`morghdari_testrun`), with the runtime image plus
`pytest`/`pytest-django`/`pytest-cov`. `test_followthrough_core.py` went from
**14 failed / 1 passed** to **17 passed**.

**One caution worth recording:** the first verification of that fix was wrong. I
re-ran against a stale `test_`-prefixed database that pytest had not recreated,
concluded the fix did not work, and very nearly reverted it. Drop **both**
`morghdari_testrun` and `test_morghdari_testrun` before judging a cold-DB fix.
