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

- BUG-5 closed: LoginView now carries LoginRateThrottle (auth_login 60/min per attempted username; IP-keying rejected — shared test IP false-triggers). New ackend/tests/test_m2_11_login_throttle.py; throttle class in pps/monitoring/ops_throttles.py; rate in config/settings/base.py.
- BUG-7 closed: compat consumer accepts both armSlug (route kwarg) and arm_slug (canonical) spellings — every compat WS connect used to KeyError (pps/compat/consumers_compat.py).
- BUG-8 closed: WS connect no longer depends on redis timing — post-auth redis I/O under one 2.5s wall-clock bound, fail-open degrade, _authorize() redis-free; 1s socket knobs on the ws redis client (pps/telemetry/ws_consumer.py, pps/telemetry/ws.py).
- BUG-20 fixed: muted 	hi crashed the drain (UnboundLocalError — 	hi computed only in the unmuted else while 	rack_breach ran either way). Muted = note+SKIP with lane-3 semantics (pps/alerts/services/evaluation.py); RED/GREEN 	est_thi_muted_suppresses_without_crashing in 	ests/test_m4_09_capability.py.
- BUG-22 fixed (regression the BUG-8 fix introduced): socket_timeout: 1 on CHANNEL_LAYERS killed every idle WS ~2s after connect (channels_redis receive() is a brpop_timeout=5 long-poll). Layer now keeps only socket_connect_timeout: 1 (config/settings/base.py); pinned by 	ests/test_channel_layer_config.py.
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
