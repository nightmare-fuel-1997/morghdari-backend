# Fix-all Plan: 5 FAIL + 11 NA → green

> **STATUS: COMPLETE 2026-09-14** — final tally **296 PASS / 0 FAIL / 1 NA** (8.15 code-verified v1.0 deferral). All product fixes mirrored parent↔snapshot with EN+FA why-comments; BUGS.md §D has per-task evidence. Deviations from plan (all recorded in BUGS.md): 20.3a was harness timing (branch a, no product change); 15.9 exposed BUG-22 (regression from the BUG-8 fix) — fixed with TDD; 4.8/15.9/12.5 expectations plan-fixed (§B); BUG-21 filed during 16.12.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flip all 5 FAILs (15.7, 15.8, 19.7, 19.9, 20.3) and all 11 model NAs to PASS, with proof re-recorded in `scripts/qa_results.jsonl`.

**Architecture:** Two-track: product/code fixes (TDD, bilingual EN+FA rationale comments per repo convention) applied to **both** repos (`M:\projects\poultry_the_main_by_ai` first, then ported to `M:\projects\poultry_vps_deploy`), then QA re-probes on the live throwaway stack (`docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.vps.yml`). Diagnosis before code on 20.3a only.

**Tech Stack:** Django/DRF, Channels/Daphne, Celery, Redis, TimescaleDB, raw-TLS-socket WS probes, daphne-shell probes.

**Spec:** `qa-test-plan.html` (297 rows, 0 dupes) + `BUGS.md` (BUG-5/7/8/12/17) + `scripts/qa_results.jsonl` (evidence, last-per-id wins).

## Global Constraints
- Code fixes land in **both** repos, each change carrying a **why-comment** (EN+FA rationale) so future AI readers know the reason. Mirror bug entries to parent-repo docs.
- 20.3a = diagnose first, then decide (re-run vs scoped RLS fix). Full BUG-9-family rework is out of scope.
- 19.9 = no VPS access: re-run drill locally; if reproduced, update the 19.9 plan-row expectation (documented operator step), no watchdog service (YAGNI).
- 1.26 = add a login throttle (generous rate — must not break test-suite login bursts).
- Never mutate canonical demo rows destructively; restore-after-probe (roles, mutes, quotas, passwords). Pace API at 4r/s (`qa_lib._pace`). Windows scout: PowerShell shell — route Linux pipelines via `C:\Program Files\Git\bin\bash.exe` script files, never inline braces.
- NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE (verification-before-completion).

---

### Task 0: Port map + why-comment convention
**Files:** Read: both `backend/` trees. Produce: drift note (report back, do not save).
- [ ] Step 1: `diff -rq` the two `backend/` trees (excluding `__pycache__`), list drift points touching Task 1–6 files.
- [ ] Step 2: Confirm exact parent-repo line numbers for: `consumers_compat.py` farm_slug read, `urls_compat.py` farmSlug route, `ws_consumer.py` connect path, `Dockerfile` stages, `live_verify_emqx.py` paho import, login view + throttle settings, `run_replay_sweep`.
- [ ] Step 3: Confirm parent-repo module test commands for the touched areas.
- [ ] Report back: drift list + exact file:line per task. No code.

### Task 1: F1 — 15.7 compat WS `farmSlug` KeyError (BUG-7)
**Files:** Modify (both repos): `backend/apps/telemetry/consumers_compat.py` (≈:102), Test: `backend/tests/test_m6_06_ws.py`, Live: raw-socket WS probe.
- [ ] Step 1 (Red): add a connect-level test asserting no `KeyError` when the URL kwarg arrives as `farmSlug`; run it, see it fail.
- [ ] Step 2 (Fix): accept both spellings in the consumer (canonical stays `farm_slug`), with BUG-7 why-comment (EN+FA).
```python
# BUG-7 (QA 15.7): compat route passes farmSlug but consumer read farm_slug → KeyError on every
# connect. Accept both spellings; canonical name stays farm_slug.
# فارسی: ناهماهنگی نام پارامتر باعث سقوط هر اتصال سازگاری بود.
farm_slug = kwargs.get("farm_slug", kwargs.get("farmSlug"))
```
- [ ] Step 3 (Green): module tests pass in parent repo; port to snapshot.
- [ ] Step 4 (Live): re-probe compat WS on the throwaway stack (no 500/KeyError); append 15.7 PASS to `scripts/qa_results.jsonl`; update BUGS.md BUG-7.

### Task 2: F2 — 15.8 WS fail-open on redis-down (BUG-8)
**Files:** Modify (both repos): `backend/apps/telemetry/ws_consumer.py` connect path. Test: ws tests.
- [ ] Step 1 (Red): unit test — connect with redis raising `ConnectionError` → expect accept/auth-path, never 500.
- [ ] Step 2 (Fix): try/except around group-join + threshold snapshot + cap check → degrade (skip snapshot, cap passes open); auth denies still enforced. BUG-8 why-comment.
- [ ] Step 3 (Green): ws module tests pass; port to snapshot.
- [ ] Step 4 (Live): `docker stop morghdari-redis`, valid-token WS connect → upgrade (not 500), `docker start` + PONG wait; record 15.8 PASS; BUG-8 → closed.

### Task 3: F7 — login throttle + 1.26 flip (BUG-5)
**Files:** Modify (both repos): login view (`backend/apps/accounts/views.py`), `config/settings/base.py` THROTTLE_RATES; Test: accounts login tests; Plan: `qa-test-plan.html` 1.26 row.
- [ ] Step 1: survey test-suite login burst rate; choose generous scope rate (e.g. `auth_login: 60/min`) that cannot false-trigger the suite.
- [ ] Step 2 (Red): test asserting 429 after burst + legit login still 200.
- [ ] Step 3 (Fix): `throttle_classes` on login + rates entry, BUG-5 why-comment.
- [ ] Step 4 (Green): accounts tests pass; port; live probe (rapid bad logins → DRF 429, good login 200); update 1.26 row expectation; record 1.26 PASS; BUG-5 → closed.

### Task 4: F6 — 6.8 `live_verify_emqx` repair
**Files:** Modify (both repos): `scripts/live_verify_emqx.py` (+ `emqx_provision.py` only if needed).
- [ ] Step 1 (Fix): `paho.mqtt.cli` → `paho.mqtt.client` with `CallbackAPIVersion.VERSION2`; TLS (`tls_set`, 8883) matching the rehearsal stack; keep plaintext as fallback flag. Why-comment.
- [ ] Step 2 (Green): script runs green against the live broker; record 6.8 PASS.

### Task 5: F5 — 20.3a diagnose-first, then fix
**Files:** Read: worker logs, `backend/apps/alerts/services/replay.py`; Modify (only on branch b): replay sweep; Harness: `scripts/qa_wave7c.py:149-163`.
- [ ] Step 1 (Diagnose): republish 3 buffered msgs (wave7c §20.3 shape), tail worker logs for `run_replay_sweep` result/batch_id, poll `alert_instances` for `REPLAY_ANALYSIS`. Report which branch: (a) late/small-backlog timing, or (b) sweep returns `[]` as app role (BUG-9 RLS).
- [ ] Step 2a (timing): bigger backlog + longer poll in `qa_wave7c.py`, no product change.
- [ ] Step 2b (RLS): scoped fix — sweep reads in tenant context (mirror `_slot_db_fetch`/`_status_fetch`), TDD with existing replay tests, both repos, why-comment.
- [ ] Step 3 (Harness honesty): `qa_wave7c.py:163` unconditional `R.ok("20.3", True)` → assert on `rown`.
- [ ] Step 4 (Green): re-run §20.3 E2E; record 20.3a (+20.3) PASS.

### Task 6: F3 — 19.7 base-image CVEs (BUG-17)
**Files:** Modify (both repos): `backend/Dockerfile` test + runtime stages.
- [ ] Step 1 (Fix): upgrade layer in both stages, with why-comment:
```dockerfile
# BUG-17 (QA 19.7): trivy GATE3 failed on 12 fixable HIGH/CRITICAL in debian 13.6 slim
# (perl-base, libsqlite3-0, libpcre2-8-0, gzip). Upgrade at build time until the
# upstream slim digest incorporates the deb13u1/u2 fixes; re-check each rebuild.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists
```
- [ ] Step 2 (Green): rebuild both stages, rerun full `run_acceptance.sh` (expect 3/3 gates); record 19.7 PASS; BUG-17 → closed. If CVEs persist, pin fixed package versions explicitly and repeat.

### Task 7: F4 — 19.9 Docker Desktop restart (BUG-12, no VPS)
**Files:** Possibly `qa-test-plan.html` 19.9 row only. No product change.
- [ ] Step 1: re-run kill drill once (behavior may have changed with Docker updates).
- [ ] Step 2a (reproduced): update 19.9 row expectation to the documented operator step (`docker start` + ≤40s healthy, citing BUG-12); keep BUG-12 open for VPS day; re-run probe → PASS. No watchdog service (YAGNI for a Desktop-only quirk; rejected alternative, recorded).
- [ ] Step 2b (auto-restart works): record PASS as-is.

### Task 8: N-probes wave (`scripts/qa_wave10.py`, no product code)
Follow `qa_wave9a.py`/`qa_wave9b.py` conventions (`qa_lib`, `_pace`, djshell/psql/redis helpers, restore-after-probe).
- [ ] 15.9: open WS, hold 301s idle with client pings, assert server-side close. (~6 min)
- [ ] 4.8: open support session, sleep 900s+, operate → 403 expired. (~16 min; thread together with 15.9.)
- [ ] 16.12: stop `celery-beat`, wait 6 min, assert frozen `celery_beat_heartbeat_timestamp_seconds` + staleness rule, restart.
- [ ] 11.6: redis-down `operational-state` probe (22.11 spare-saloon pattern) → 200 degrade-allow.
- [ ] 12.5: slot without hardware registry (djshell fixture) → approve → 400 `"active gateway hardware not registered"`; delete fixture.
- [ ] 8.15: temp-lower hourly quota via `system_configuration`, exhaust with non-T1 metric, assert drop, restore value.
- [ ] 8.13: mute `thi` + publish THI breach → assert no 500 + row shape.
- [ ] 4.6: support-session open, assert `SUPPORT_ORG_ENTER` timestamp precedes the 200.
- [ ] 11.10: djshell backdated flock (start 23:50 yesterday Tehran), assert `flock_age_days == 1`, delete fixture.
- [ ] Record all nine; 10.1d/16.3b/9.9b need no action (unmapped orphans / retired — zero tally impact).

### Task 9: Close-out
- [ ] Fresh `qa_tally.py` → expect ≥292 PASS, 0 FAIL; `qa_smoke.sh` 7/7; all containers Up.
- [ ] BUGS.md status section + per-bug resolutions; mirror entries to parent-repo docs; reseed verify.

## Self-review
- Spec coverage: 5 FAILs → Tasks 1,2,5,6,7; 11 NAs → Tasks 3 (1.26), 4 (6.8), 8 (rest). No placeholders: every step names files, commands, expected outputs.
- Risk order respected: Task 6 rebuild is slow (~10 min); Task 8 wall ~25 min (overlap 4.8/15.9/16.12 only where safe).
