# Findings — v0.5 Provisioning + Zero-Touch

## Contract discoveries

- `versions/v0.5/TASKS.md` orders M5-01 through M5-10. M5-01 is the dependency spine: every provisioning status change must use one atomic transition service.
- The v0.5 theme is the trust boundary: PENDING_APPROVAL telemetry is buffered, approval drains FIFO, rejection/suspension blocks it, and credential rotation commits only after device ACK.
- Installer sync must derive hardware identity server-side, force PENDING_APPROVAL, and return 409 + Retry-After for a same-gateway concurrent sync.
- Review permission is platform-side and extensible; default reviewers are system admin/platform support, not organization roles.

## Repository discoveries

- Current branch before branching: `demo/v0.4-showcase`.
- New branch created: `v0.5-m5`.
- The checkout already contains substantial uncommitted v0.4 demo/backend/UI work and generated runtime artifacts. These are pre-existing and must not be discarded or broadly staged.
- Django apps already include `provisioning`, `devices`, `mqtt_auth`, `telemetry`, `alerts`, `auditing`, and `system_config`; existing Redis, DRF, Celery, bcrypt, and tenant middleware conventions should be reused.
- Existing telemetry consumer and alert code explicitly defer several v0.5 surfaces, so implementation should replace those seams rather than invent parallel abstractions.
- Existing `PROGRESS.md` is the historical v0.1–v0.4 mirror and contains non-English legacy text; new visible documentation authored in this task remains English per project policy.

## Baseline risk

- Because the working tree is dirty, all edits must be narrow and tracked independently. Validation should focus on files changed for v0.5 and avoid commands that rewrite generated/runtime artifacts.

## 2026-09-14 QA fix round (poultry_vps_deploy live-stack validation; fixes mirrored here)

Why this section exists: the VPS deploy rehearsal (poultry_vps_deploy) ran the full 297-row QA plan against a live Docker stack; five FAILs plus deferred NAs produced product fixes that were applied HERE first (TDD) and mirrored byte-identical to the snapshot. Each code change carries an EN+FA why-comment naming its BUG id so future readers (human or AI) know why the code changed.

- BUG-5 closed: LoginView now carries LoginRateThrottle (auth_login 60/min per attempted username; IP-keying rejected — shared test IP false-triggers). New `backend/tests/test_m2_11_login_throttle.py`; throttle class in `apps/monitoring/ops_throttles.py`; rate in `config/settings/base.py`.
- BUG-7 closed: compat consumer accepts both `farmSlug` (route kwarg) and `farm_slug` (canonical) spellings — every compat WS connect used to KeyError (`apps/compat/consumers_compat.py`).
- BUG-8 closed: WS connect no longer depends on redis timing — post-auth redis I/O under one 2.5s wall-clock bound, fail-open degrade, _authorize() redis-free; 1s socket knobs on the ws redis client (`apps/telemetry/ws_consumer.py`, `apps/telemetry/ws.py`).
- BUG-20 fixed: muted `thi` crashed the drain (UnboundLocalError — `thi` computed only in the unmuted else while `track_breach` ran either way). Muted = note+SKIP with lane-3 semantics (`apps/alerts/services/evaluation.py`); RED/GREEN `test_thi_muted_suppresses_without_crashing` in `tests/test_m4_09_capability.py`.
- BUG-22 fixed (regression the BUG-8 fix introduced): socket_timeout: 1 on CHANNEL_LAYERS killed every idle WS ~2s after connect (channels_redis receive() is a brpop_timeout=5 long-poll). Layer now keeps only socket_connect_timeout: 1 (config/settings/base.py); pinned by `tests/test_channel_layer_config.py`.
- BUG-21 filed (not fixed): Prometheus's Django job targets daphne:8000 but DJANGO_ALLOWED_HOSTS lacks daphne -> every scrape DisallowedHost; BeatHeartbeatStale unmonitorable in practice until the host list widens.
- Full evidence and the remaining environment-leaning items (BUG-12 Docker Desktop restart quirk, BUG-18 farm/saloon RESTRICT-audit delete family, 8.15 v1.0 quota deferral) live in poultry_vps_deploy/BUGS.md; final tally 296 PASS / 0 FAIL / 1 NA of 297.


## 2026-09-15 bug-sweep fix round (branch `fix/bug-sweep-2026-09-15`)

Closes BUG-1, BUG-2, BUG-6, BUG-9, BUG-9b, BUG-10, BUG-11, BUG-13, BUG-14,
BUG-15, BUG-18, BUG-21. Full ledger: `BUGS.md` at the repo root. Regression
tests: `backend/tests/test_bugfix_2026_09_15.py`. Commits `6ebd61e` + `768840d`.

- BUG-9: sweeps cross-org reads now use the `admin` (BYPASSRLS) alias.
- BUG-9b: `clear_active_pointer()` clears `alert:active:*` on force-resolve and sweep-resolve.
- BUG-2: `/reconciliation/` defaults to yesterday Tehran.
- BUG-6: WS deny/cap accept-before-close.
- BUG-11: `gateway_config` resolves the slot in a tenant context.
- BUG-10/18: audit farm/saloon FK -> SET_NULL (+ migration).
- BUG-13: drain dead-letters unprocessable entries.
- BUG-14: `deploy.sh` nginx reload. BUG-15: wal-g creds. BUG-21: `.env.example` daphne. BUG-1: `DEPLOYMENT.md` migrate note.

## 2026-09-15 open-bug fix round (all green)

Why this section exists: the remaining open bugs from the VPS live-stack QA (BUG-1/2/4/6/9b/10/11/13/18/21 + BUG-9 proof) were fixed HERE first (TDD) and mirrored byte-identical to the snapshot, each with EN+FA why-comments. Final QA: 296 PASS / 0 FAIL / 1 NA (8.15, quota-engine deferral); parent full acceptance re-run 650 passed / 3 skipped / 0 failed.

- BUG-9 closed: enforced-RLS proof tests (backend/tests/test_sweep_rls_enforced.py, module-scoped admin-alias fixture) + the last real gap found thereby — write_maintenance_mute_digests() now discovers via _admin_alias() (apps/alerts/services/mutes.py).
- BUG-9b closed: clear_active_blob() + coalesce self-heal for stale pointers, no TTL by design (would duplicate the one-active-row invariant); force-resolve clears the blob (apps/alerts/services/firing.py, apps/accounts/views.py).
- BUG-10/18 closed: AccountActivityLog farm/saloon FKs RESTRICT -> SET_NULL (+ reversible migration); fresh entities delete 200, history stays honestly 409.
- BUG-6 closed: WS connect() accept-then-close — real 4003/4000 wire codes (apps/telemetry/ws_consumer.py); plan rows reworded from wire-403.
- BUG-11 closed: DATABASE_ADMIN_URL added to the daphne service (docker-compose.prod.yml) — the admin alias previously existed only on celery-worker so device-config discovery 404'd in request traffic.
- BUG-21 closed: scrape proven (django job up); DisallowedHost resolved via the daphne ALLOWED_HOSTS entry.
- BUG-2/4/13 closed by re-probe on the verified-fresh image (default-yesterday 200; smoke-6; poison -> DLQ PROCESSING_FAILED + XACK).
- BUG-13 port (2026-09-15, second pass): the drain quarantine lived only in the snapshot tree and was never mirrored here — ported byte-identical (apps/telemetry/consumers.py: per-row isolation + delivery-count quarantine) PLUS a parse guard the snapshot lacked (unparseable fields DLQ with PROCESSING_ERROR + ack, preserving this repo's older behavior instead of escaping into infinite redelivery). Tests: test_malformed_entry_dead_letters_with_processing_error + test_persistent_db_failure_quarantined_after_threshold (parent CI drain file 7/7); live-proven on the snapshot stack.
- BUG-11 unification (2026-09-15): this repo's tenant-context gateway_config variant answered anonymous/bad-creds with 404, contradicting the documented 401. Unified onto the admin-alias version (apps/provisioning/api.py + new admin_alias() in apps/tenants/context.py); gateway-config tests 22/22 green. All 16 fix files now byte-identical across both repos (verified).
- Test-hygiene incident (for future AI): a test module mutating settings.DATABASES at import flipped every _admin_alias() session-wide and failed 47 modules; fixed with module-scoped fixture lifecycle. Never mutate settings at import time.
- Image-hygiene incident (for future AI): 'up -d --build' silently no-ops the build — always separate 'build [--no-cache]' + check Built + in-container content check by md5, never grep markers alone.
- Still open: BUG-12 (needs Linux VPS kill-drill; runbook in snapshot DEPLOYMENT.md section 7).
