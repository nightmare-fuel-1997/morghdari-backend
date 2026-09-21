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
| EMQX JWT harness | MED | mqtt_auth tests | synced JWT-login + chain-probe broke 10 EMQX-path tests; unreachable probes are no longer cached as verdicts (soft-fail preserved, `reachable` stays honest for the ceremony) + fakes emulate login/token + present chain | `backend/apps/mqtt_auth/services.py`, `backend/tests/test_m5_06_07_08_enforcement.py`, `backend/tests/test_m5_02_03_provisioning_api.py` | full gate: **655 passed / 3 skipped @ 90.12%** + module gate GREEN + migrate-gate (reached via ritual order); image-scan env-blocked (trivy DB download EOFs on this network, twice) |

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

---

## Carried forward from the VPS deploy working copy (merged 2026-09-21)

> These entries existed only in the server-side QA findings document and were absent
> from this ledger. They are reproduced here verbatim so the fixes and the one OPEN
> item are not lost with the deploy copy. Source: `BUGS.md` on the deployed tree
> (`/opt/morghdari`), section F onwards. The full server document is archived in the
> merge delivery folder under `incoming/server/`.
## F. VPS day 1 - deploy blockers found on the live host (2026-09-16, 185.8.175.150)

> Session: bring-up of `vps-01` on Ubuntu 24.04 / Docker 29.7.2. Every entry
> below was reproduced, fixed and re-verified on the host; full evidence
> (commands, before/after, timestamps) is in the day-1 report and in
> `/root/morghdari-ops-2026-09-16/change-log.md` on the server. Same table
> format as section A: id | severity/state | ref | description.

| Bug | Severity | Ref | Detail |
| --- | --- | --- | --- |
| BUG-23 | **HIGH (fixed)** | VPS-1 | **`tls_issue_vps.sh` could never run as documented.** Line 14 read `CERTIFICATE_EMAIL` from the process environment, but the runbook invokes the script with `sudo` and Ubuntu's sudoers carries `Defaults env_reset` - an exported shell variable never reaches it, so `export CERTIFICATE_EMAIL=... && sudo bash scripts/tls_issue_vps.sh` always died with `line 14: CERTIFICATE_EMAIL: export CERTIFICATE_EMAIL first`. Proven on the host with an A/B probe (same variable: without sudo `sees: probe@example.com`, with sudo `sees: <empty>`). Fix: the script sources `.env` (explicit environment values still win), like `prod_bootstrap.sh` already did; `README_VPS.md` A2 no longer documents the broken form. |
| BUG-24 | **HIGH (fixed)** | VPS-1 | **No MQTT client could complete a TLS handshake, silently.** The A7.1 copy step preserves certbot's `0600 root:root` on `privkey.pem`/`server.key`; the EMQX container runs as `uid=1000(emqx)` and got `Permission denied` reading the key, so `ssl:default` accepted TCP and dropped every handshake (`shutdown_count: [{ssl_error,269}]`, subscribers logging `Connection reset by peer`) while the listener still reported `running` - and both subscriber healthchecks (process scans) stayed green. This would have broken the REAL Let's Encrypt certificate and re-broken MQTT at every 60-day renewal. Fix in `tls_issue_vps.sh`, `tls_issue_vps_dns.sh` and `reload-tls.sh`: `chgrp 1000` + `chmod 0640` on the two key files (nginx runs as root and keeps read access). |
| BUG-25 | **HIGH (fixed)** | VPS-1 | **`prod_bootstrap.sh` aborted on a fresh volume.** Step 2 granted privileges on tables that migrations had not created yet (`GRANT SELECT ON mqtt_device_credentials, mqtt_acl_rules, gateway_slots TO emqx_auth` -> `ERROR: relation "mqtt_device_credentials" does not exist`), and `set -e` killed the whole script there - so the subscriber service accounts were never seeded either. Fix: the table-dependent grants and the seeds are conditional on `to_regclass(...)`, print exactly what was deferred and why, and the script exits 0 both before and after migrate (idempotent). |
| BUG-26 | **MEDIUM (fixed)** | VPS-1 | **The app image cannot be built where pypi.org is unreachable.** Measured on the host: `pypi.org/simple/` returned nothing at 15 s or 30 s, first byte at 45 s (plain HTTP also timed out), while `files.pythonhosted.org` answered in 0.27 s; `pip wheel` failed with `No matching distribution found for Django<5.2,>=5.1.14`. Fix: `backend/Dockerfile` takes an opt-in `ARG PIP_INDEX_URL` (writes `/etc/pip.conf` only when set) and `docker-compose.vps.yml` forwards `${PIP_INDEX_URL:-}` from `.env` - unset means stock PyPI, so CI and other hosts are unaffected. |
| BUG-27 | **MEDIUM (open - decision)** | VPS-1 | **`EmqxQueueDropped` can never fire.** The rule reads `emqx_client_queue_dropped_total`; EMQX 5.5.1's own Prometheus endpoint (`/api/v5/prometheus/stats`, fetched live: HTTP 200, 15 706 bytes) exports `emqx_delivery_dropped_*`, `emqx_messages_dropped*`, `emqx_packets_publish_dropped` and `emqx_vm_process_messages_in_queues`, but no series with that name. Needs either a corrected metric name or a deliberate "dormant" note. Related, not a bug: `redis_up`, `subscriber_up` and `probe_success` need redis_exporter / a subscriber metrics producer / blackbox-exporter, none of which this repo's composes ship. |
| BUG-28 | **INFO (fixed)** | VPS-1 | **Secret hygiene on the deploy host.** `.env` was world-readable (`644 root:root`) while holding every production secret, ten of its values were still the literal `change-me` (including `POSTGRES_ADMIN_PASSWORD`, which is baked into the postgres role at first initdb - so it had to be replaced BEFORE the first `up`), and two editor leftovers (`.env.save`, `..env.swp`) were also `644`. Fixed: `.env` is `600`, the ten values are `openssl rand -hex 32` output, leftovers are `600`. |
| BUG-12 | **CLOSED (drill was wrong)** | VPS-1 | Re-verified on Linux with three controlled tests: `docker kill` leaves the container `exited` with RestartCount 0 (a user-initiated stop suppresses the restart policy on every platform, so the documented drill can never pass); `kill -9 1` from *inside* the container does nothing at all (a PID-namespace init ignores in-namespace signals); `kill -9 <host pid>` produced `die -> start` in 4 s with a healthy container and API 200. `DEPLOYMENT.md` A7 now documents the host-PID drill and the verdict. |

### Environment findings (not code - they cannot be fixed from the repo)

| Finding | Impact | Action |
| --- | --- | --- |
| Inbound TCP/80 and /443 are filtered **upstream of the host** (provider firewall). The host answers 200 locally and from 3 of 8 external vantage points; Let's Encrypt's validators timed out twice. `ufw` is inactive and `iptables` INPUT is ACCEPT. | Blocks the real certificate, and later the public API/MQTT path | Open 80 + 443 in the hosting provider's firewall panel |
| `mqtt.<domain>` has no A record | A two-name certificate cannot be validated | Create the record and set `MQTT_DOMAIN`, or keep the single-name certificate the script now issues |
| No WAL archive target configured (`WALG_S3_PREFIX`/`AWS_*` empty) | `pg_wal` grows ~3-4 GB/day (measured 48 MB -> 96 MB in 42 min) and PITR is unavailable | Set the storage variables, then install the weekly `walg_base_backup.sh` cron line |
