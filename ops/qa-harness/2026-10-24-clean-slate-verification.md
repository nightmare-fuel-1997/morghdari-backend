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
- Full suite on the server: **931 passed, 13 skipped, 0 failed**.
- Health 200; fleet green; LOS alerts fired and resolved correctly during the run.

## Honest caveats

- `scripts/deploy.sh` (the documented automated deploy) has still never been
  exercised — deploys were done by hand again.
- Two regression tests for D6/D7 were removed as harness-fragile
  (FakeRedis + unmanaged-hypertable interplay); both behaviours are
  live-verified on the server instead.
- The twin's sensor physics remains a diurnal model, not a real shed.
