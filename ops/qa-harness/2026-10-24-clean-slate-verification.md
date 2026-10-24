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

### What is still untouched

Offline buffering across long outages, replay/backfill, subscription and
maintenance mutes, the THI and velocity alert lanes, WebSocket, report/PDF
export, and the frontend contract. The twin's sensor physics is still a diurnal
model, not a real shed.
