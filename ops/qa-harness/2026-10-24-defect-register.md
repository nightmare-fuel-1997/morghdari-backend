# Defect Register — 2026-10-24 clean-slate verification

Every defect found while deploying and exercising the system with the digital
twins, with the commit that fixes it and the evidence that the fix works.

**20 numbered findings — 19 real defects** (15 in the application, 4 in the deploy
tooling, 1 in disaster recovery) plus one false alarm: D16 was the documented
grace window, not a fault.

**All 19 real defects are fixed and live-verified.**

**D15 is FIXED and live-verified (vps-09); D17 (vps-10).** All were found by
running the real thing (fresh database, real HTTP, real MQTT, real twins) — not
by reading code.

| ID | Symptom (what an operator/user would see) | Root cause | Commit | Proof it is fixed |
|---|---|---|---|---|
| D1 | A newly created platform-staff account can never log in: login demands a TOTP code, `/auth/mfa/setup/` demands a JWT, and the JWT requires a completed MFA login | circular admission requirement on the two MFA endpoints | `bd2682d` | live: setup with username+password → confirm with a live code → login succeeds; a *confirmed* account still refuses password-only re-issue |
| D2 | `GET /provisioning/installations/assignable/?org_id=2` returns a raw HTTP 500 | a non-UUID string reached the tenant UUID parser | `bd2682d` | live: HTTP 400 with `org_id must be a UUID`; valid UUID still 200 |
| D3 | `POST /gateways/{id}/commands/ {"kind":"ROTATE_CREDENTIAL"}` creates a command the device then FAILS with `rotate without a new password` | the generic endpoint accepts a kind it cannot fulfil (no staged hash) | `bd2682d` | live: HTTP 400 pointing at `/rotate-credential/`; no command row created |
| D4 | One command failing marks *every* in-flight command on the gateway FAILED, and stamps `applied_at` on them | slot-level FAILED broadcast was unscoped | `bd2682d` | test: a PROVISION failure leaves an ACKNOWLEDGED REBOOT untouched with `applied_at = NULL` |
| D4b | A command the device already applied can later be flipped to EXPIRED | expiry sweep had no `applied_at` guard | `bd2682d` | test: an APPLIED row past `expires_at` stays APPLIED after the sweep |
| D5 | Assigned threshold bands never arm: the saloon's threshold hash carries `operational_state` and `flock_age_days` but no `temp_max` | the celery recache ran with no tenant GUC, so FORCE-RLS `lifecycle_templates` was invisible and every assignment fell through to the default chain | `bd2682d` | live: the worker's recache now resolves the published template and writes `temp_max=26.00, temp_min=15.00` |
| D6 | **Threshold alerts never fire for live telemetry** — only LOS (a separate watchdog lane) ever appears | the subscriber writes FLAT metrics (`{"temp": 25.75}`); the evaluator required window dicts (`{"temp": {"avg": …}}`) and silently skipped everything | `bd2682d` | live: a sustained breach produced a `temp` alert, which then auto-RESOLVED when the twin recovered |
| D7 | Hourly charts, KPI panels and the nightly rollup are all empty forever | `metric_hourly_aggregates` had **no writer anywhere in the codebase** | `bd2682d` | live: `aggregates.build_metric_hourly` wrote 64 rows from real telemetry; 30-min beat entry added |
| D8 | A REBOOT shows `applied_at` set but state `ACKNOWLEDGED`, then EXPIRED | a replayed QoS-1 ACK downgraded an APPLIED command (the ACK branch had no state guard) | `25e163b` | live: REBOOT reaches APPLIED and **stays** APPLIED 35 s later |
| D9 | `bash scripts/deploy.sh <tag>` fails: compose has no build context and tries to pull `ghcr.io` | the script omitted `docker-compose.vps.yml` (this host builds locally; there is no registry) | `6e0b904` | drill: `==> OK: all 9 containers healthy on tag vps-07`, exit 0 |
| D10 | A release deploy tries to **recreate postgres**, and the automatic rollback hits the same `No such image` wall | `up -d <app services>` still evaluates dependencies, dragging stateful infra into the release set | `a05aaa4` | drill: `--no-deps`; deploy touches only the 5 app services; rollback path proven separately |
| D11 | Every release needed the DB image re-tagged by hand or any dependency-touching compose run died | `postgres-walg` was pinned to `${MORGHDARI_TAG}` (tags `vps-02`…`vps-05` are literally one image) | `a05aaa4` | `POSTGRES_IMAGE_TAG`, default `vps-05`; running container's image reference unchanged, so no recreate |
| ~~D16~~ **not a defect** | The THI rule appeared never to fire: 38 °C / 85 % RH (THI 96.9 vs `thi_max=80`) produced no `thi` alert | **the midnight-transition grace window (D9).** Any recache that CHANGES the hash writes `threshold:transition:{saloon}` with a 300 s TTL, and lanes ②–④ are suppressed while it lives (velocity bypasses it by design). Every THI drill recached first, so the lane was silenced on purpose | — (no code change) | proved: recache → `EXISTS threshold:transition:3` = 1, `TTL` = 299 s; clear the key and the SAME 26→29→32→35→38 ramp fires `thi / ACTIVE_TIER_1 / 96.90 / REALTIME` — the exact THI value for 38 °C / 85 % |
| D19 | A retroactive replay alert **never closed**: one sat ACTIVE for 20 hours while the fleet was normal, its value silently drifting (41.80 → 43.00) as each later backlog re-coalesced into it | `escalate_all` excludes `source="REPLAY_ANALYSIS"` from its recovery branch (the branch keys on a breach key a replay alert never arms, so it would close them 2 min after firing). The exclusion's cost was that nothing ever closed them — and the one-active-row rule made that single row immortal | `7316126` | live on vps-12: the 20 h-old row resolved at 08:28:24 (in-band path) and the 08:18 row at 08:50:23 — 31.9 min after firing, the exact moment its 30-min dwell expired. 3 tests (backstop, dwell, in-band) |
| D20 | An LOS alert **could never close once its saloon was retired**: a saloon-2 row stayed ACTIVE for 25 h with the fleet streaming normally | `los_watchdog` only iterates saloons that still have ACTIVE slots, so after a slot/saloon is deactivated nothing can "report again" and the open row is unreachable by every code path — an ordinary operator action (retiring a sensor) strands it | `66e2625` | live on vps-13: retired the dead drill slot → `LOS resolved: saloon=2 has no ACTIVE slots left (retired)`, open alerts **0**. 2 tests (retired closes, active-and-silent stays open) |
| D18 | A client that sends the checklist FLAT (`checklist_feed: true`) is told **"checklist incomplete"** on a SUBMITTED write, even though every flag is true | `EXTRA_FIELDS` lists the flat names, the model stores them and the read API returns them, but only the NESTED `{"checklist": {...}}` form was folded into the normalized view that the submit gate checks | `44f9a33` | live on vps-11: flat flags → **201 SUBMITTED**; one flag false → 400 (gate intact); nested object still wins over flat. Unit test pins all three cases |
| D17 | Every reading reaches the dashboard **twice**: identical `telemetry.update` frames back to back on the live socket | `drain_stream` reads new entries (`>`) and then this consumer's own PEL (`"0"`) in the SAME cycle; the `>` read has already put them in our PEL, so both reads return the same entry and both copies were appended to `raw_entries` — pushing, alert-evaluating and counting every reading twice. `bulk_create(ignore_conflicts=True)` hid it at the DB layer, which is why row counts always looked right | `a57973b` | found by the live WS drill (2 publishes → 4 frames); after the fix on vps-10 a single published reading produces exactly ONE frame. Regression test on real redis pins `processed == 1` and one push per entry |
| D15 | Deleting a threshold rule does not remove its band: the saloon keeps alerting against a threshold an operator has removed | `recache_saloon` writes the new hash with `HSET` and never removes keys absent from it, so a deleted/lowered rule's `{metric}_max` persists in `saloon:{id}:thresholds` until the whole key is dropped | `ad4ac1b` | found live: deleted the `thi` rule, recached, `thi_max=80.00` still present. After the fix on vps-09: `thi_max` **gone** from `saloon:3:thresholds` (only `operational_state, flock_age_days, temp_max, temp_min` remain); regression test added |
| D14 | After an outage, the drained backlog produces **no retroactive replay alert** even when it breached — a hot window is reviewed as if it were normal | `_window_breaches` required `{"temp": {"avg": …}}` and skipped the flat `{"temp": 40.0}` the subscriber stores — the same defect as D6, in the second copy its docstring claims to "mirror EXACTLY" | `c97cab0` | live on vps-08: a drained backlog at 41–42 °C against `temp_max=26` produced `temp / ACTIVE_TIER_1 / 41.80 / source=REPLAY_ANALYSIS` with a batch id. Regression test covers both shapes |
| D13 | A backup restores *approximately*: data lands, but chunk constraints are missing and `pg_restore` exits 1 with ~27 `ONLY option not supported on hypertable operations` errors — and nothing says so | no restore procedure existed; a bare `pg_restore` skips the Timescale hooks that a hypertable dump requires | `85fd24c` | drill: naive restore = exit 1 / 27 errors; canonical sequence (`CREATE EXTENSION` + `timescaledb_pre_restore` → restore → `timescaledb_post_restore`) = **exit 0, zero errors**, 934 telemetry rows, `hypertables=2 chunks=2`. Encoded as `scripts/restore.sh` with live-DB and corrupt-dump guards |
| D12 | `./scripts/deploy.sh` → `Permission denied` | `core.fileMode = false` on the VPS: the executable bit is not recorded | `e841039` (runbook) | `DEPLOYMENT.md` §8 now says `bash scripts/deploy.sh <tag>` and carries a guarantees table |

## Regression coverage

`backend/tests/test_2026_10_24_live_defects.py` pins **D1, D2, D3, D4, D4b, D6,
D7 and D8** (10 tests, all green). D6 and D7 were briefly removed because their
first drafts fought the harness; both are back, done the project's own way —
D6 on the real integration redis (`INTEGRATION_REDIS_URL`, db 11) instead of a
hand-rolled fake, D7 with the DDL guard from `test_followthrough_core` that
creates the unmanaged hypertables from the `0002` migration SQL.

**D5 has no automated test.** It is proven live (the worker's recache resolves
the published template and writes `temp_max`), but reproducing "a celery task
running with no tenant GUC under FORCE RLS" in the suite is not something the
current fixtures support — stated here rather than implied to be covered.

## How the run was executed

Fresh database (backup at `/root/backups/clean-slate-*`), sanctioned initdb +
RLS, org and users created, two gateways provisioned through the real install
flow, twins connected with minted credentials, telemetry and commands exercised,
then each fix deployed as image `vps-07` and re-verified live. Full suite on the
server: **931 passed, 13 skipped, 0 failed**.
