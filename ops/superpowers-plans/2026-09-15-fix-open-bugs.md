# Fix-All Open Bugs Implementation Plan

> **STATUS: COMPLETE 2026-09-15** — final tally **296 PASS / 0 FAIL / 1 NA** (8.15). Parent acceptance **650 passed / 0 failed**. All product fixes mirrored with why-comments. Deviations recorded in BUGS.md: mutes.py was the 4th BUG-9 gap (fixed); acceptance-RED was test-hygiene poison (fixed, no product change); 17.1/12.7 early FAILs were stale-image artifacts (ENV-3); 11.9 reworded for the new delete rule.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every open bug (BUG-1, 2, 4, 6, 9/9b, 10/18, 11, 12, 13, 21) with TDD code fixes mirrored parent↔snapshot, live re-probes on the throwaway stack, and QA records updated.

**Architecture:** Investigation showed BUG-1/2/4/11/13 and the BUG-9 sweep reads are already fixed in code — they need live re-probes, not code. Genuinely open code work: BUG-9b (blob lifecycle), BUG-10/18 (audit-FK decoupling), BUG-6 (accept-then-close), BUG-21 (ALLOWED_HOSTS). BUG-12 stays a VPS-day item.

**Tech Stack:** Django/DRF, Channels/Daphne, Celery, Redis, TimescaleDB, raw-TLS-socket WS probes, daphne-shell probes.

**Spec:** `BUGS.md` + `qa-test-plan.html` (297 rows) + `scripts/qa_results.jsonl` (last-per-id wins).

## Global Constraints
- User decisions (binding): BUG-9b = full lifecycle (force-resolve clearing + coalesce self-heal, NO TTL — a TTL could duplicate the one-active-row invariant mid-episode); BUG-10/18 = decouple audit FK; BUG-6 = accept-then-close.
- Every product change lands in BOTH `M:\projects\poultry_the_main_by_ai` (first, TDD) AND `M:\projects\poultry_vps_deploy` (byte-identical mirror), each with an EN+FA why-comment naming its BUG id.
- Parent CI: `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build test pytest <file> -v -o addopts="--reuse-db --nomigrations --strict-markers"`. Live stack: `docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.vps.yml` (snapshot workdir). API `https://localhost/api/v1`, demo-owner/password12345, ≤4r/s. Windows: PowerShell default shell; Linux pipelines only via `C:\Program Files\Git\bin\bash.exe` script files in `.tmp-qa/`.
- NO completion claims without fresh run evidence. No commits (no remotes).
- **ENV-3 (2026-09-15, load-bearing):** `up -d --build` silently no-ops the build (shows Starting/Started, no build lines) — observed a 12h-stale image served as fresh. EVERY image build MUST be: (1) a separate `docker compose build [--no-cache] <svc>` step, (2) check output for "Built", (3) in-container content check via PowerShell direct `docker exec <svc> grep -c <marker> /app/<file>` (Git Bash mangles leading /app paths — never check via bash). No live verification is valid without step 3.
- **ADDENDUM 2026-09-15:** (i) Task B follow-up: `write_maintenance_mute_digests()` (mutes.py:72-75) reads on default alias with no tenant_context — the one remaining zero-row gap as app role; micro-fix with the same `_admin_alias()` pattern (TDD, the failing test already exists in `test_sweep_rls_enforced.py`). (ii) BUG-11 TRUE fix: `DATABASE_ADMIN_URL` exists only on celery-worker (both prod composes:284) — daphne has no `admin` alias so `admin_alias()` falls back to "default" and device-config discovery 404s in request traffic. Fix: add the same env line to the daphne service in both prod composes + amend base.py's "Never used by request traffic" comment for the device-config exception; recreate daphne (env-only, no rebuild); re-run wave11.

---

### Task A: Re-probe round — already-fixed bugs (no product code)
**Files:** Create `scripts/qa_wave11.py`. Touch nothing else. Follow `scripts/qa_wave9b.py` conventions (qa_lib, djshell/psql/redis helpers, admin DB alias under FORCE RLS).
- [ ] Step 1 — BUG-2: `GET /reconciliation/` with no `?day=` → expect 200 (code defaults to `tehran_yesterday()`, `backend/apps/monitoring/ops_views.py:1482`). Record `16.13a` PASS.
- [ ] Step 2 — BUG-4: run `vps_smoke.sh` leg 6 on Git Bash → expect PASS (shim at script:44-49). Record `smoke-6` PASS.
- [ ] Step 3 — BUG-11: device-Basic fetch `GET /gateways/GW-QA0001/config/` (Basic `GW-QA0001:GW-QA0001-mqtt-pw`) → 200; manager JWT → 200; bad creds → 401. Record `12.7` group PASS.
- [ ] Step 4 — BUG-13: publish a poison entry (NUMERIC-overflow `recv_ts`, per BUG-13; quarantine path `backend/apps/telemetry/consumers.py:437-541`, config `realtime_dlq_after_deliveries`) → assert DLQ entry with reason + XACK via redis-cli (bounded poll, no infinite redelivery). Record `17.1` variant PASS.
- [ ] Step 5 — run + verify: all PASS, `qa_smoke.sh` still 7/7.

### Task B: BUG-9 residual proof — enforced-RLS sweep tests (tests only)
**Files:** Create `backend/tests/test_sweep_rls_enforced.py` (parent first, then identical mirror). Consumes: `seeded_rls_db` fixture + `test_rls.py` SET ROLE pattern.
- [ ] Step 1 — write 4 tests (escalate_all / los_watchdog / write_maintenance_mute_digests / run_replay_sweep): seed open rows as admin, then `SET ROLE morghdari_app` with no org GUC and run the job — it must find/process rows (the old code saw ZERO rows here).
```python
@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_escalate_finds_rows_as_app_role(timescale_db, seeded_rls_db):
    ...seed one unresolved breach alert + breach key as admin, then:
    with connection.cursor() as c:
        c.execute("SET ROLE morghdari_app")
        try:
            assert escalate_all() >= 1
        finally:
            c.execute("RESET ROLE")
```
- [ ] Step 2 — run in parent CI; any failure = a remaining BUG-9 gap → fix that job's read path with the admin-alias pattern before proceeding.
- [ ] Step 3 — mirror test file to snapshot. Green = BUG-9 discovery layer proven closed.

### Task C: BUG-9b full blob lifecycle (code, both repos, TDD)
**Files:** Modify `backend/apps/alerts/services/firing.py` (add `clear_active_blob()` + coalesce self-heal), `backend/apps/accounts/views.py` (`support_alert_force_resolve` clears blob). Tests: extend firing + support-activation test files (find exact files first: likely `tests/test_m4_04_firing.py` + `tests/test_m7_07_support_activation.py`).
- [ ] Step 1 — Red tests (parent): (i) force-resolve an open alert → re-breach same saloon+metric → assert a NEW row fires (`action == "fired"`); (ii) hand-plant a blob pointing at a RESOLVED row → fire → assert fresh row + blob replaced.
- [ ] Step 2 — implement (parent):
```python
def clear_active_blob(redis_client, saloon_id: int, metric: str) -> None:
    """Drop the open-alert pointer (BUG-9b full lifecycle). Called by
    force-resolve AND by the coalesce self-heal below — the sweep's resolve
    branch keeps its own delete (untouched).
    No TTL backstop by design: a TTL could expire mid-episode and a re-fire
    would insert a SECOND active row for the same saloon+metric, violating
    the one-active-row invariant the coalescer exists to protect.
    فارسی: بدون TTL — انقضای میانهٔ دوره ردیفِ تکراری میسازد؛ ترمیم در نقطهٔ خواندن."""
    redis_client.delete(active_alert_key(saloon_id, metric))
```
In `fire_or_coalesce`, after loading `existing`: if the pointed-to row `resolved_at is not None` → `clear_active_blob(...)` + fall through to fresh fire. In `support_alert_force_resolve` after the resolved_at save → `clear_active_blob(system_redis(), alert.saloon_id, alert.metric_type)` (import `system_redis` from `apps.alerts.services.sweeps`; verify no import cycle first).
- [ ] Step 3 — green parent CI, mirror identical to snapshot.
- [ ] Step 4 — live E2E (snapshot): sustained temp breach on Saloon-A → alert → staff force-resolve → breach again → SECOND row fires. Evidence → BUGS.md BUG-9b closure note.

### Task D: BUG-10/18 decouple audit FK (code, both repos, TDD)
**Files:** Modify `backend/apps/auditing/models.py` (`AccountActivityLog.farm/saloon` → `SET_NULL`), new migration, `backend/apps/farms/services_follow.py` (guard comment update). Tests: farms delete tests.
- [ ] Step 1 — survey: list every FK to Farm/Saloon with RESTRICT/PROTECT; decouple ONLY audit-class (`AccountActivityLog.farm/saloon`, plus `command_logs` equivalents if same shape — verify table). Domain FKs (flock, telemetry, reports, members) stay strict. Report the list.
- [ ] Step 2 — Red tests (parent): (i) API-create farm+saloon, zero history → `DELETE saloon` → 200, `DELETE farm` → 200, `*_CREATED` audit rows survive with NULL FK + intact `details`; (ii) saloon with closed flock → 409 `saloon has flock history`; (iii) farm with saloon → 409 `farm not empty`.
- [ ] Step 3 — implement: `on_delete=RESTRICT` → `SET_NULL, null=True` (already nullable) + reversible migration + why-comment. Update `saloon_delete_error` docstring (fresh entities truly deletable now).
- [ ] Step 4 — green parent CI (farms + auditing + migrate-gate), mirror to snapshot.
- [ ] Step 5 — live: API create→delete fresh saloon/farm → 200s. Update `qa-test-plan.html` 11.9d-adjacent text only if it contradicts the new rule (read first).

### Task E: BUG-6 accept-then-close (code, both repos, TDD)
**Files:** Modify `backend/apps/telemetry/ws_consumer.py` `connect()` (+ compat consumer if it shares the deny path — check). Docs: OpenAPI WS description + Flutter contract doc (find first). Tests: existing WS deny tests.
- [ ] Step 1 — implement (parent): keep `_authorize()` exactly as-is (pure, redis-free, fail-closed); `accept()` FIRST, then deny → `close(code=verdict)` keeping the AUTH-DENY log. Accepted-anonymous sockets close immediately with a code — never join groups, never get snapshots.
- [ ] Step 2 — green parent CI (in-process Communicator tests assert the same codes — must still pass).
- [ ] Step 3 — docs + mirror + rebuild daphne on the live stack.
- [ ] Step 4 — live + plan rows: raw-socket probes (no-token/bad-token/wrong-org/over-cap) → expect 101 then close codes (4003/4000 family), not 403. Grep `403` in §15/§21 plan rows first; update every row asserting wire-403 (15.2 group, 21.16); re-record them PASS on the new text.

### Task F: Small fixes + VPS-day runbook
- [ ] BUG-21 (config): add `daphne` to `DJANGO_ALLOWED_HOSTS` in snapshot `.env` (+ parent `.env` example if present); restart daphne; assert Prometheus Django scrape returns 200 and no new DisallowedHost lines; note in DEPLOYMENT.md. No product code.
- [ ] BUG-1/BUG-4 (verify-only): confirm README_VPS migrate block matches the `prod_bootstrap.sh` admin path (close on doc evidence); BUG-4 proven by Task A step 5's smoke run.
- [ ] BUG-12 (runbook): append the exact VPS-day verification checklist to snapshot `DEPLOYMENT.md` (kill daphne → expect auto-restart ≤40s on Linux → if reproduced there, escalate to restart-policy fix). No code.

### Task G: Regression re-probes (after all rebuilds)
- [ ] One combined rebuild (`up -d --build` backend services + nginx reload), then: §15 WS deny group (new expectations), §11/12 farm-saloon CRUD + delete group, force-resolve + refire (Task C proof), §2 RLS spot-checks. Re-record changed rows; everything else must stay green.
- [ ] Full parent `run_acceptance.sh` (broad regression proof for mirrored code).

### Task H: Close-out
- [ ] Fresh `qa_tally.py` (target 0 FAIL), `qa_smoke.sh` 7/7, all-Up, reseed verify.
- [ ] BUGS.md per-bug resolutions + §D status; mirror entries to parent `findings.md`.
