# Clean-Slate Verification Report — 2026-10-24

**Result: the latest version is deployed on the test server, exercised end to end
with the digital twins, every defect found was fixed and re-verified live, and
the full test suite is green.**

## What was done

1. **Clean slate** — backed up (7.8 MB dump at `/root/backups/clean-slate-*`),
   dropped and recreated the DB via the sanctioned path
   (`prod_bootstrap.sh` pre/post + `initdb` RLS), 51 tables FORCE-RLS.
2. **Everything through the real API** — org + 6 users, full install flow
   (register → request → approve → assign → bind → saloon → position →
   sensor slot → pack map → submit): all PASS.
3. **Twins live** — 2 gateways provisioned via the sanctioned provisioner;
   both CONFIGURED, applied_v=3, telemetry flowing (8 metrics), bad payloads
   dead-lettered with a reason.
4. **All command kinds live** — REFRESH_CONFIG / DIAGNOSTICS_REQUEST / REBOOT
   / ROTATE_CREDENTIAL (with commit-on-ACK: old password rejected at the
   broker, new one accepted, fresh re-auth proven) plus all negative cases.

## Defects found and fixed (8)

| # | Defect | Fix |
|---|---|---|
| D1 | Platform-staff MFA first-run deadlock (setup needs JWT, login needs MFA) | password admission for unconfirmed accounts only |
| D2 | `/installations/assignable/` 500 on malformed org_id | 400 |
| D3 | Generic command endpoint accepted ROTATE_CREDENTIAL it could never fulfil | 400 + pointer to dedicated endpoint |
| D4 | Slot-FAILED broadcast failed every in-flight command and faked applied_at | PROVISION-only, no fake stamp |
| D4b | Expiry sweep could expire an already-applied command | applied_at guard |
| D5 | Celery threshold recache blind under FORCE-RLS — assigned bands never armed | admin-alias reads |
| D6 | Alert evaluator required window-dicts; live telemetry is flat — **threshold alerting was dead on the live path** | normalize once at entry |
| D7 | `metric_hourly_aggregates` had **no writer at all** — charts/KPIs/daily rollup read an empty table forever | `aggregates.build_metric_hourly` (30-min upsert) + beat entry |
| D8 | A replayed ACK downgraded APPLIED commands to ACKNOWLEDGED | late-ACK guard |

Each fix was deployed (image vps-07) and re-verified **live against the twins**.

## Final state

- Trees: local = `origin/main` = server @ `470589f`, clean; `deploy_tag=vps-07`;
  all 6 containers verified on the vps-07 image by ID.
- Full suite on the server: **933 passed, 13 skipped, 0 failed**.
- Health 200; fleet green; LOS alerts fired and resolved correctly during the run.

## Honest caveats

- Two regression tests for D6/D7 were removed as harness-fragile
  (FakeRedis + unmanaged-hypertable interplay); both behaviours are
  live-verified on the server instead.
- The twin's sensor physics remains a diurnal model, not a real shed.

## Deploy tooling drilled (same day, after the verification run)

`scripts/deploy.sh` had never been executed. Drilling it found the documented
deploy path could not have worked on this host, and fixing it caught a
data-risk bug:

| # | Defect | Fix |
|---|---|---|
| D9 | The script omitted `docker-compose.vps.yml` — no registry here, so `up -d` had no build context and fell through to a doomed `ghcr.io` pull | include the VPS override when present |
| D10 | `up -d <app services>` still evaluates **dependencies**, so a release deploy dragged **postgres** into the recreate set — and the automatic rollback hit the identical `No such image` wall | `--no-deps`: app deploys are app-tier only |
| D11 | `postgres-walg` was pinned to `${MORGHDARI_TAG}` — every release needed the DB image re-tagged by hand (tags `vps-02`…`vps-05` are all one image) | own axis: `POSTGRES_IMAGE_TAG`, default `vps-05` |
| D12 | `./scripts/deploy.sh` fails with `Permission denied` (`core.fileMode=false` on the VPS) | runbook now says `bash scripts/deploy.sh` |

Drill results after the fixes:

- **Happy path** — `==> OK: all 9 containers healthy on tag vps-07`, `exit=0`,
  `.deploy_tag` written, health 200.
- **Rollback path** — deploying `vps-does-not-exist` failed fast
  (`No such image`), auto-rolled back to `vps-07`, `exit=1`, `.deploy_tag`
  unchanged, all 9 containers healthy and health 200 throughout.

No container was lost in the pre-fix failures: the failed recreate left the
existing postgres container running, and all 9 stayed healthy.


---

## Follow-up round (same day): the untouched paths

### 1. Restore drill — found D13, the worst kind of DR bug

A real dump was taken and restored. **Data fidelity was perfect** (all 11 audited
tables matched the live DB exactly: 934 telemetry rows, 160 hourly aggregates,
13 commands, 7 alerts). But the obvious command is wrong on this database:

| Approach | Result |
|---|---|
| `pg_restore -d <db> dump` | **exit 1, 27 errors**, chunk FK constraints never applied |
| `CREATE EXTENSION` + `timescaledb_pre_restore()` → restore → `timescaledb_post_restore()` | **exit 0, zero errors**, `hypertables=2 chunks=2` |

Data lands either way, so a hook-less restore *looks* successful while silently
missing integrity constraints — the worst failure mode for disaster recovery.
There was no restore script in the repo at all. `scripts/restore.sh` now encodes
the canonical sequence and verifies row counts + hypertable/chunk presence,
refusing the live database without `--force` (exit 3) and refusing a missing or
unreadable dump (exit 1). Drilled through the script: `exit=0`,
"restored cleanly (pg_restore exit 0, hypertables intact)".

### 2. Fault injection — the fleet was never run dirty

The simulator's fault modes were switched on for ~6 minutes:
`drop=0.10, delay=0.10 (8 s), invalid=0.05, restart=0.02`.

| Observation | Result |
|---|---|
| twins stayed connected and kept reporting | sent 4 and 6 windows, faults=2 and faults=3 |
| telemetry still ingested | 934 → 994 rows during the window |
| malformed frames | quarantined with precise reasons: `bad_json`, `UNKNOWN_SLOT`, `TOPIC_PAYLOAD_IDENTITY_MISMATCH`, `UNSUPPORTED_SCHEMA_VERSION` |
| worker tracebacks / subscriber criticals | **0 / 0** |
| DLQ growth | +10, all classified — nothing unclassified, no crash loop |

### 3. D6/D7 regression tests restored

Both were re-added the project's own way: D6 on the real integration redis
(`INTEGRATION_REDIS_URL`, db 11) instead of a hand-rolled fake; D7 with the
DDL-guard pattern from `test_followthrough_core` that creates the unmanaged
hypertables from the `0002` migration SQL. `test_2026_10_24_live_defects.py` now
holds 10 green tests (D1, D2, D3, D4, D4b, D6, D7, D8). D5 remains live-verified
only, stated plainly rather than implied.

### 4. Offline buffering + replay — found D14

A device that loses connectivity flushes its backlog marked `is_buffered: true`;
the subscriber routes those to the **historical** lane (never realtime) and bumps
a replay counter. Drilled with a synthetic outage backlog:

| Check | Result |
|---|---|
| routing | 10 buffered frames → `telemetry:historical` (realtime untouched), drained to lag 0 |
| storage | 10 rows with `is_buffered=true`, `buffered_delay_mins` avg 33 min / max 60 min — matching the spread |
| in-band backlog | **no** replay alert (correct: retroactive analysis only reports real breaches) |
| out-of-band backlog (41–42 °C vs `temp_max=26`) | **no alert** — D14: the detector required window-dicts and skipped flat metrics |
| after the D14 fix (vps-08) | `temp / ACTIVE_TIER_1 / 41.80 / source=REPLAY_ANALYSIS` with a batch id |

This is the second copy of the D6 defect, in the function whose docstring says it
mirrors the realtime evaluator "EXACTLY". Both now share the same normalization.


### 5. Alert lanes, WebSocket, reports — the rest of the untouched list

**Velocity lane — PASS.** A 25 → 36 °C jump inside one 60 s window fired
`temp / ACTIVE_TIER_1 / 36.00` on real telemetry.

**THI lane — PASS (the earlier "D16" was my own test's fault).** The rule never
seemed to fire. It does: any recache that CHANGES the threshold hash writes
`threshold:transition:{saloon}` with a 300 s TTL, and lanes ②–④ are suppressed
while that key lives (velocity deliberately bypasses it). Every THI drill
recached first, so the lane was silenced on purpose. Proved both directions:
recache → key present, TTL 299 s; clear it → the same 26→29→32→35→38 ramp at
85 % RH fires `thi / ACTIVE_TIER_1 / 96.90` — exactly THI(38 °C, 85 %).

**Subscription mute — PASS, A/B.** With the twin stopped so nothing else could
publish: muted → **0** new temp alerts and the digest counter climbing
(40 → 52); unmuted, the identical ramp → `temp / ACTIVE_TIER_1 / 40.00`.
(The earlier "LEAKED" reading was a velocity alert from the twin's own
40 → 25 drop — velocity bypasses mutes by design.)

**WebSocket — PASS, and it found D17.** Through nginx:
`WS /ws/saloons/3/telemetry?token=…` → `101 Switching Protocols`, a
`telemetry.hello` carrying the live threshold hash, and a `telemetry.update`
for each published reading. No token → close **4003** (`AUTH-DENY
clause=no-token`). The first run showed every update arriving **twice**, which
became D17 (fixed, vps-10, re-verified: one reading → one frame).

**Reports — PASS, and it found D18.** Create (DRAFT and SUBMITTED), the
checklist gate (a false flag still 400s), the edit path (`feed_kg` 90 → 99 with
`notes` persisted), the edit-window guard (a past-date edit → 403), vet review
(SUBMITTED → **APPROVED**), list and by-date reads, and the C-14 foreign-saloon
`{"results":[],"count":0}`. There is no PDF export in this codebase — reports
are JSON CRUD — so that item on the earlier list was moot.

### Final gate

Full suite on the server, image built from HEAD (vps-13): **942 passed,
13 skipped, 0 failed** in 394 s — every pre-existing test plus the 19 in
`test_2026_10_24_live_defects.py`.

### What is still untouched

Nothing on the original list is untested now. What genuinely remains outside
this exercise:

- **Real hardware / HIL.** The twin speaks the real protocol but its sensor
  physics is a diurnal model, not a shed.
- **The frontend contract.** I can prove the API's shapes and status codes; I
  cannot prove the Flutter client renders them. The one contract change noted
  earlier (review-approve no longer returns credentials) still needs the
  frontend team's confirmation.
- **Alert forensics.** An `AlertInstance` row records metric, value, status and
  source, but NOT why it fired — a velocity emergency and a sustained breach
  are indistinguishable once the Redis blob is cleared. Worth adding a `reason`
  column when the schema is next opened.

---

## Overnight soak (16 h unattended, 2026-10-24 19:27 → 2026-10-25 11:40)

The stack was left running with both twins and nobody watching. This is the
first evidence that it stays correct on its own, not just when driven.

| Check | Result |
|---|---|
| container restarts (9 containers) | **0** — daphne/celery/beat/subscribers up 16 h, nginx/redis 11 days, emqx 3 days |
| telemetry ingestion | **exactly 240 rows/hour, every hour** (4 slots × 60) — no gaps, no drift |
| stream lag / pending | realtime **lag 0, pending 0** (308 258 entries read); historical lag 0 |
| DLQ | stable at 12 — nothing new dead-lettered |
| hourly aggregates (D7 builder) | **32 rows per hour = 4 slots × 8 metrics, every hour** |
| daily rollup | present (32 rows) |
| midnight rollover | `flock_age_days` advanced 0 → 1 by the recache |
| worker / subscribers / daphne errors | **0 / 0 / 0** over the whole window |
| health | 200 throughout |

Two defects came out of it, both the same family — **a row that no code path
can close**:

**D19** — a retroactive replay alert sat ACTIVE for 20 hours, its value drifting
from 41.80 to 43.00 as each new backlog re-coalesced into it. `escalate_all`
excludes `REPLAY_ANALYSIS` on purpose (its recovery branch keys on a breach key
a replay alert never arms, so it would close them two minutes after firing) —
but nothing else closed them either, and the one-active-row rule made that row
immortal. They now close after a 30-min visible dwell once the saloon is back in
band, or at a 24 h backstop. Live: the 20 h row resolved at 08:28:24; the 08:18
row resolved at 08:50:23 — 31.9 min after firing, the moment its dwell expired.

**D20** — an LOS alert for a saloon whose slots were retired could never close:
`los_watchdog` only visits saloons that still have ACTIVE slots, so nothing was
ever going to "report again". Retiring a sensor while its LOS alert is open is an
ordinary operator action, so the watchdog now closes those orphans. Live: after
retiring the dead drill slot, `LOS resolved: saloon=2 has no ACTIVE slots left`
and open alerts went to **0**.

Bonus evidence from the same window: saloon 3's LOS episodes at 08:38→09:06 and
10:13→10:15 fired **and** auto-resolved on their own — the LOS lifecycle works
unattended, which was previously only observed once.

---

## Pass 2 — every write action, end to end (2026-10-26)

Pass 1 swept all 129 routes for "does every handler run" (128 probes, 0 defects,
32 action endpoints deferred because they ACT on an empty body). Pass 2 drives
every one of those actions with a real payload and the role that is allowed to
call it: `ops/qa-harness/api_actions.py`.

**Result: 75 checks, 75 passed, 0 failed** against the live deployment on
`vps-14`, with every resource created by the run disposed of or deactivated
afterwards.

| Area | Actions exercised |
|---|---|
| geography | provinces / cities / villages |
| farm → saloon → position → sensor slot | create, rename, update, capabilities |
| flock lifecycle | create, mortality, feed, water, weight samples, close |
| reports | create DRAFT, edit, read by date |
| panels + dashboard | `/panels/me/`, role-permission set/delete, user-override set/delete, personal + role dashboard config |
| mutes | subscription mute/unmute, maintenance mute create/revoke |
| org members | invite, role change, deactivate |
| gateway | suspend → resume, REBOOT, DIAGNOSTICS_REQUEST, config, list |
| gateway requests | reject stale (cap-aware), create, approve → installation jobs |
| factory units | register, claim-code re-issue, credential rotation, list, retire |
| hardware registry | register + retire a sensor (inside the support session) |
| support ops | open session, force-resolve an alert |
| alerts | acknowledge a live alert |
| auth | token refresh, session list, session revoke, password change (+ the similarity-validator refusal), login with the new password |
| threshold templates | create, update, publish |

Two real defects came out of the pass, both fixed and live-verified: **D21**
(above — the worst of the two: a resumed gateway silently stops answering
commands) and the CI gate's missing mounts (below).

### The CI gate could not pass on this host, and it said nothing about the code

Running the deploy gate on the VPS surfaced two environment faults that made a
green tree look broken:

| Fault | Why it happened | Fix |
|---|---|---|
| `range of CPUs is from 0.01 to 2.00` | `docker-compose.ci.yml` caps the test service at **2.5** CPUs; the VPS has 2 | run with a 1.5-CPU override on this host |
| 3 failures + 2 collection errors in `test_celery_queue_coverage` / `test_sim_fleet` / `test_sim_twin` | the CI image packages only `./backend`, so the repo-root `docker-compose.prod.yml` (where the worker's `-Q` list lives) and `simulator/` were never mounted — and those tests deliberately FAIL rather than skip when the file is missing | `docker-compose.ci.yml` now mounts both (`337b7ec`) |

After the fix the gate is green: **914 passed, 16 skipped, 0 failed** in 462 s,
coverage **88.88 %** (floor 80 %), module coverage gate GREEN.

### What pass 2 deliberately does not do

- **`POST /api/v1/logout/` is not a product endpoint.** It is DRF's
  browsable-API session logout (CSRF form view from `rest_framework.urls`), so
  it is excluded; session revocation is `DELETE /auth/sessions/{id}/`, which is
  exercised.
- **An invited member can never log in.** `POST /org/members/` creates the user
  with an UNUSABLE password by design (credential handover only), so the
  password drill runs on the owner fixture.
- **A saloon that has held a flock cannot be deleted** (`409 saloon has flock
  history`), and a farm with saloons is `409 farm not empty`. The pass asserts
  the refusal rather than a deletion — that is the designed audit rule. The QA
  org therefore accumulates one inert farm + saloon per run.
- **Org members have no DELETE** — `PATCH` (role) and `POST /deactivate/` only,
  so cleanup deactivates.
