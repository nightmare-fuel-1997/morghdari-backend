# BUGS - unfixed-bug ledger (Morghdari backend)

> Canonical bug list for `M:\\projects\\poultry_the_main_by_ai`.
> Source of record: the QA round in `M:\\projects\\poultry_vps_deploy\\BUGS.md`
> (297-row plan; fix round closed 2026-09-14). The prior round
> (BUG-5/7/8/16/17/19/20/22) was already fixed and is carried on this branch.
>
> Fix branch: `fix/bug-sweep-2026-09-15`. Commits: `6ebd61e` (fixes + tests),
> `768840d` (migration graph). Last updated: 2026-09-15.

## Status summary

| Status | Count |
|---|---|
| Fixed this round | 12 |
| Open / blocked | 4 |
| Deferred by design | 2 |
| Already closed (carried) | 8 |

## Fixed this round

| ID | Sev | Area | Fix | Files | Commit | Verification |
|---|---|---|---|---|---|---|
| BUG-1 | HIGH | deploy docs | Document the admin-URL migrate required on a fresh volume (app role lacks CREATE on `public`, PG15+) | `DEPLOYMENT.md` | 6ebd61e | section added (`## Migrate on a fresh volume (BUG-1)`) |
| BUG-2 | MED | monitoring API | `GET /reconciliation/` with no `?day=` now defaults to yesterday Tehran instead of 500 | `backend/apps/monitoring/ops_views.py`, `ops_services.py` | 6ebd61e | `pytest tests/test_m9_recon.py tests/test_bugfix_2026_09_15.py` -> 10 passed; counterfactual `_tehran_day_bounds(None)` raises TypeError (the old 500 mechanism) |
| BUG-6 | MED | telemetry WS | Deny/cap paths now `accept()` before `close()` so 4003/4000 reach the wire (was a bare HTTP 403 handshake) | `backend/apps/telemetry/ws_consumer.py` | 6ebd61e | regression test fails pre-fix, passes post-fix |
| BUG-9 | CRIT | alerts engine | Cross-org sweep reads (escalation, LOS watchdog) route through the `admin` (BYPASSRLS) alias, like replay/rebuild_state | `backend/apps/alerts/services/sweeps.py` | 6ebd61e | `py_compile` OK; unit test asserts the helper; runtime reproduction needs PG (not run in this env) |
| BUG-9b | CRIT | alerts engine | `alert:active:*` blob cleared on force-resolve and on sweep-resolve so a metric can page again | `backend/apps/alerts/services/sweeps.py`, `backend/apps/accounts/views.py` | 6ebd61e | unit test on `clear_active_pointer`; `py_compile` OK |
| BUG-10 | MED | farms API | Saloon hard-delete reachable: audit `saloon` FK is now SET_NULL | `backend/apps/auditing/models.py`, `apps/auditing/migrations/0002_alter_accountactivitylog_farm_and_more.py` | 6ebd61e, 768840d | model-meta test asserts SET_NULL; migration graph has a single leaf |
| BUG-11 | HIGH | provisioning API | `gateway_config` resolves the RLS'd slot inside a tenant context (device-credential org, else manager JWT org) | `backend/apps/provisioning/api.py` | 6ebd61e | `py_compile` OK; runtime needs PG (not run in this env) |
| BUG-13 | MED-LOW | telemetry drain | Unprocessable entries are dead-lettered (`PROCESSING_ERROR`) and acked instead of retried forever | `backend/apps/telemetry/consumers.py` | 6ebd61e | `py_compile` OK; runtime needs PG+Redis (not run in this env) |
| BUG-14 | HIGH | ops/deploy | `deploy.sh` reloads nginx after recreating app containers (stale upstream -> 502) | `scripts/deploy.sh` | 6ebd61e | `bash -n` OK |
| BUG-15 | LOW | DR scripts | `walg_base_backup.sh` ships PG credentials | `scripts/walg_base_backup.sh` | 6ebd61e | `bash -n` OK |
| BUG-18 | MED | farms API | Farm hard-delete reachable: audit `farm` FK is now SET_NULL (same family as BUG-10) | `backend/apps/auditing/models.py` | 6ebd61e | model-meta test |
| BUG-21 | LOW | monitoring config | `.env.example` documents the `daphne` scrape host (fresh deploys otherwise get DisallowedHost) | `.env.example` | 6ebd61e | grep BUG-21 |

## Open / blocked

| ID | Sev | Area | Status | Reason | Workaround | Next action |
|---|---|---|---|---|---|---|
| BUG-12 | MED | infra | Open (env-leaning) | `docker kill morghdari-daphne` leaves the container exited with RestartCount 0 - a Docker Desktop quirk; not lastingly fixable without the Linux VPS | operator runs `docker start` manually (gap is a bounded 502/429) | re-verify on the real Linux VPS; a watchdog service was rejected (YAGNI) |
| BUG-4 | LOW | QA harness | Open (sibling repo) | `vps_smoke.sh` calls bare `python3`, which resolves to the Windows Store stub - lives in `poultry_vps_deploy`, outside this repo | run via a `python3` shim | fix in the QA project |
| BUG-8 residual | LOW | telemetry WS | Open | after a redis-down fail-open degrade the socket closes soon after the degraded hello (the channel layer's receive needs redis) | clients reconnect automatically | implement a rejoin-fallback task |
| BUG-13 (batch) | LOW | telemetry drain | Partially open | the new guard wraps `_to_model_kwargs`; a metric value that overflows the DB column would still fail the whole per-org `bulk_create` batch | - | add a per-row fallback on `bulk_create` failure |

## Deferred by design

| ID | Area | Reason | Owner |
|---|---|---|---|
| 8.15 | quotas | hourly message-quota enforcement deferred to the v1.0 quota engine; metering-only today | product / v1.0 |
| C-16 | test infra | reference-catalog starvation in combined runs; root channel unidentified; contained by a conftest guard | platform |

## Already closed (carried on this branch)

BUG-5, BUG-7, BUG-8, BUG-16, BUG-17, BUG-19, BUG-20, BUG-22.

## Verification log (2026-09-15)

- `python -m py_compile` on all 8 changed Python files -> OK
- `bash -n scripts/deploy.sh scripts/walg_base_backup.sh` -> OK (exit 0)
- `pytest tests/test_m9_recon.py tests/test_bugfix_2026_09_15.py -o addopts= -q` -> 10 passed
- BUG-6 counterfactual: regression test fails on pre-fix code (1 failed), passes post-fix (1 passed)
- Migration graph: `auditing` has exactly one leaf (`0002_accountactivitylog_fk_set_null`)
- Full CI gate `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build test` -> see the fix report

Not run in this environment (needs the live Postgres + Redis stack, unavailable here
and not startable quickly): the full pytest suite, and the runtime reproduction of
BUG-9/9b/11/13/14. Those rest on code inspection + the QA project's own live evidence.
