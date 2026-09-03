# v0.5 — Provisioning + Zero-Touch *(M5)*

> **Theme:** the trust boundary — unapproved hardware can never inject data.
> **Risk killed:** trust boundary.
> **Maps to:** Blueprint **M5** · **Duration:** ~1–2 weeks

## What this version delivers

- **Gateway-request flow** (Step 0): FM submits → platform-side review (System Admin /
  Platform Support by default via an EXTENSIBLE platform permission — GS-BOARD), cap
  from config; approval creates slot + MQTT credentials
- Installer offline PWA sync (`POST /provisioning/sync/`, installer-role scoped):
  zone assignments, sensor mappings (hardware derived server-side), replacements —
  flips status → PENDING_APPROVAL
- FM approve/reject (+ `provisioning_audit_log`, status-cache invalidation in the same
  transaction); approve dispatches `drain_pending_approval`
- Zero-touch config endpoint: `GET /gateways/{id}/config/` Basic-auth against
  `mqtt_device_credentials` (bcrypt), rate-limited by nginx zone, org_id/farm_id in
  payload, slot_ids deliberately ABSENT (R2)
- Pending buffer semantics: PENDING_APPROVAL telemetry held in
  `telemetry:pending_approval`; REJECTED/SUSPENDED discarded; EMQX ACL allow-rows +
  no_match deny enforced per provisioning state
- **Staged credential rotation**: pending_password_hash → swap on gateway ACK
  (`ROTATE_CREDENTIAL` command envelope), bcrypt only (EMQX-native), never emailed

## Exit demo

Unapproved gateway's telemetry sits in pending · approve → drains in order · reject →
discarded · suspended gateway fails at EMQX auth · installer cannot self-approve ·
rotation ACK-commit dance end-to-end with the simulator · R3/R4/R6 suites green.

## Files

[TASKS.md](TASKS.md) · [WALKTHROUGH.md](WALKTHROUGH.md) · [TUTORIAL.md](TUTORIAL.md)

## Status — 2026-09-02

M5-01..09 implemented and green on the CI gate; M5-10 partially done (docs
swept, tag + recorded demo pending owner). See [TASKS.md](TASKS.md) for
per-task status.

## Test matrix

| Suite | Acceptance | Result (Docker pg16/Redis, 2026-09-02) |
|---|---|---|
| M5-01 transition service | audit + status + cache invalidation atomic | ✅ `test_m5_01_transition.py` (3) |
| M5-02/03 gateway requests + installer sync | role caps, hardware derived, PENDING_APPROVAL | ✅ `test_m5_02_03_provisioning_api.py` (3) |
| M5-04 trust boundary | held FIFO / DLQ archive / legacy flows | ✅ `test_m5_04_05_trust_boundary.py` (7 routing) |
| M5-05 FM lifecycle | approve drains, reject archives, 403/404/409 | ✅ `test_m5_04_05_trust_boundary.py` (6 API) |
| M5-06 gateway config | pre/post approval 200, rejected/suspended 403 | ✅ `test_m5_06_07_08_enforcement.py` |
| M5-07 EMQX ACL | allow-rule push, revoke-to-empty, FNV-soft + authn-user sync | ✅ `test_m5_06_07_08_enforcement.py` + **live EMQX 5.5.1** (`scripts/live_verify_emqx.py`) |
| M5-08 rotation | stage/commit-on-ACK/wrong-password 409/stale 24 h + broker password push | ✅ `test_m5_06_07_08_enforcement.py` + **live EMQX 5.5.1** (`scripts/live_verify_emqx.py`) |
| M5-09 concurrency | 409 + Retry-After, zero partial topology | ✅ `test_sync_conflict_returns_retry_after` |
| Full CI gate | 80% coverage floor | ✅ **284 passed, 3 skipped, 89.24%** |
| Real-EMQX CI job | M5-07/08 broker semantics never only mocked | ✅ `docker compose -f docker-compose.ci.yml run --rm live-verify` (emqx service + provisioner + 10/10 live checks) |

## Deployment notes

- **Env**: `DATABASE_URL` / `REDIS_URL` carry everything (single-env
  convention); subscriber additionally requires `SUBSCRIBER_CLIENT_ID` and
  uses `MQTT_HOST`/`MQTT_PORT`/`MQTT_USERNAME`/`MQTT_PASSWORD`.
- **EMQX**: `apps/mqtt_auth/services.py` talks to the 5.x management API
  (`EMQX_API_URL`/`EMQX_API_USER`/`EMQX_API_PASSWORD`, defaults
  `http://127.0.0.1:18083` demo-admin/dev_password). Run
  `scripts/emqx_provision.py` to provision demo authn users + per-farm ACL
  rules (shapes pinned by `test_emqx_provision.py`); broker `no_match=deny`
  is a bootstrap setting. ACL sync is FNV-soft: broker failure is logged and
  the `mqtt_acl_rules` tables remain the source of truth.
- **Rate limiting**: config payload carries a `rate_limit_note` — throttle
  MQTT CONNECT + publish per gateway at nginx (`limit_req` keyed on client
  ip); broker-level quotas arrive with the v1.0 quota engine.
- **Rotation**: plaintext password is issued exactly once (approval or
  `rotate-credential`); later config fetches return only the bcrypt hash +
  `rotation_pending` flag.

## Look-back

Did the per-slot sync lock hold under concurrent installers (409 path)? Do real
transitions match the Blueprint §11 state matrices? Did rotation's envelope
discrimination rule hold (and defer during replay)?
- ✅ Lock: `test_sync_conflict_returns_retry_after` green; no partial writes.
- ✅ Matrices: `test_wrong_state_transitions_conflict` — illegal transitions
  raise 409 with zero status/audit writes.
- 🔶 Rotation envelope discrimination (command-channel version) deferred to
  the v0.3 command channel — v0.5 ships the REST stub.
