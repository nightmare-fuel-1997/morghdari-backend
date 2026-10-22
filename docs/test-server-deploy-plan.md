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

## 9. Runbook — progress and exact resume point

Updated as steps complete. **Everything up to the blocked step is done and
verified; nothing is broken and nothing is half-applied.**

| # | Step | State |
|---|---|---|
| 1 | Inspect, change nothing | **done** — see §5 |
| 2 | Back up | **done** — `/root/backups/predeploy-20261022/` (7.2 MB dump, `.env`, `.sim.env`, deployed SHA) |
| 3 | Fetch + checkout | **done** — `/opt/morghdari` fast-forwarded `8031059` → `e375892`, tree clean, containers untouched |
| 4 | Build the image | **started, unverified** — `nohup docker build --target runtime -t ghcr.io/morghdari/backend:vps-03 backend/`, log at `/tmp/build-vps03.log` |
| 5 | Rehearse migration on a copy | not started |
| 6 | Migrate for real + re-apply `initdb/01_roles.sql` and `02_rls.sql` | not started |
| 7 | Re-seed subscriber ACLs (`subscriber-01`, `subscriber-02`) | not started |
| 8 | Re-seed the publisher credential | not started |
| 9 | Recreate app services with `vps-03`, `--no-build`, health-gate | not started |

**Blocked at:** SSH to `82.115.17.252:22` stopped accepting connections during
step 4. Diagnosed from outside:

| Port | State |
|---|---|
| 22 (SSH) | **TCP dropped** |
| 80 / 443 (nginx) | open — `https://…/api/v1/health/` returns `{"status":"healthy","version":"0.8-DR",…}` with database and Redis `ok` |
| 8883 (broker) | open — TLS handshake succeeds, `TLS_AES_256_GCM_SHA384` |
| 18083 / 8000 | not exposed (expected) |

ICMP ping succeeds throughout. So the host, the application and the broker are
all healthy; **only SSH is unreachable**, which is the signature of an IP-level
block (fail2ban or a provider firewall) rather than a server fault. I made ~20
rapid SSH connections in a short window, which is the usual trigger.

**What to do to resume** — any one of these:

1. Wait for the ban to expire (commonly 10 min – 1 h) and retry; the build will
   have finished by then and step 4 just needs verifying.
2. From another shell on the server, check and clear it:
   `fail2ban-client status sshd` then `fail2ban-client set sshd unbanip <ip>`.
3. If it is the provider's firewall rather than fail2ban, allowlist this machine's
   IP or use the server's console/VNC.

**Then continue at step 4's verification:**

```
tail -5 /tmp/build-vps03.log
docker images ghcr.io/morghdari/backend:vps-03 --format "{{.Tag}} {{.Size}}"
```

and proceed to step 5. Nothing already done needs repeating, and no step that
has run can have left the system in a worse state than it is now — the running
containers are still on `vps-02` with the old schema.

---
