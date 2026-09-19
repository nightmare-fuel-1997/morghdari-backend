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
| Fixed 2026-09-19 sweep | 1 |
| Open / blocked | 0 |
| Deferred by design | 2 |
| Already closed (carried + 2026-09-19 review) | 11 |

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

None — BUG-8 residual (the last one) closed below on 2026-09-19.

## Fixed 2026-09-19 sweep

| ID | Sev | Area | Fix | Files | Verification |
|---|---|---|---|---|---|
| BUG-8 residual | LOW | telemetry WS | late group-join on next inbound message after a degraded hello (`telemetry.rejoined` + fresh thresholds, 10 s cooldown, never raises) + `joined` outcome plumbed through `_post_auth_setup` so healthy sockets never regreet | `backend/apps/telemetry/ws_consumer.py`, `backend/tests/test_ws_rejoin_fallback.py` | 3 new tests green + `test_m6_06_ws` (18) + failopen/hang + drain/bugfix/recon/channel-layer all green on the CI PG/Timescale stack (24 + 20 in the two runs) |

## Deferred by design

| ID | Area | Reason | Owner |
|---|---|---|---|
| 8.15 | quotas | hourly message-quota enforcement deferred to the v1.0 quota engine; metering-only today | product / v1.0 |
| C-16 | test infra | reference-catalog starvation in combined runs; root channel unidentified; contained by a conftest guard | platform |

## Already closed (carried on this branch)

BUG-5, BUG-7, BUG-8, BUG-16, BUG-17, BUG-19, BUG-20, BUG-22.

## Closed 2026-09-19 review (no farm / no S3 needed — evidence cited, no code change)

- **BUG-12 — CLOSED (drill was wrong).** Sibling `BUGS.md:112`: three controlled
  Linux tests showed `docker kill` (user-initiated stop) suppresses the restart
  policy on every platform, `kill -9 1` inside the container is ignored
  (PID-namespace init), and `kill -9 <host pid>` recovers in ~4 s with API 200.
  `DEPLOYMENT.md` A7 documents the host-PID drill. VPS-2 `FINAL_STATUS.md` adds
  5 host-PID kill drills + 2 real reboots, all self-recovered. The Desktop-only
  quirk needs no watchdog (YAGNI stands).
- **BUG-4 — CLOSED (sibling repo).** Sibling `BUGS.md:14`: fixed 2026-09-15 —
  `vps_smoke.sh:44-48` resolves the interpreter via
  `command -v python3 || command -v python`, plus the `qa_smoke.sh` shim.
  Nothing in this repo to change.
- **BUG-13 (batch) — CLOSED (already implemented).** The ledger row predates the
  2026-09-15 second-pass port: `_persist_org_batch`
  (`backend/apps/telemetry/consumers.py:455-512`) already retries a failed
  per-org `bulk_create` row-by-row and quarantines only the poison entries,
  `_quarantine_repeated_failures` DLQs after the delivery threshold, and
  `test_m3_05_drain.py::test_persistent_db_failure_quarantined_after_threshold`
  pins the NUMERIC-overflow case (DLQ + XACK). Row retired, not re-fixed.

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
