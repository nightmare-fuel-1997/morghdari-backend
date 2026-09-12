# v0.8-DR DEMO — disaster day, backend half (2026-09-12)

M8-07 close evidence: the DR half of v0.8 exercised against the REAL rehearsal
stack (`docker-compose.prod.yml` + `rehearsal` override, laptop-as-VPS).
Backend-only close per owner scope cut (M8-04/05/06 parked as `v0.8-HIL`) —
the real-radio exit demo is NOT claimed here. Recording waived by owner
(v0.5 precedent); this transcript + `DR-LOG.md` are the demo.

## 0. Stack

- Postgres on the rebuilt wal-g image (`ghcr.io/morghdari/postgres-walg:rehearsal`):
  `archive_mode=on`, `archive_command=wal-g wal-push`, `archive_timeout=300`,
  MinIO S3 target (`morghdari-wal` bucket), base `base_000000010000000000000008`
  pushed, WAL segments verified flowing, timeline consistent.
- All rehearsal services healthy post-drills; 0 pending migrations (migrated
  to current tree as ADMIN: alerts.0005, farms.0002, tenants.0005).
- Demo Farm intact: org 9f84f4ea + 3 farms + 6 saloons + mappings verified
  (earlier "empty" reads were RLS hiding rows without the org GUC — data was
  never lost). Tenant token minted via the M7-13 pattern
  (`LoginSerializer.get_token` over the admin BYPASSRLS connection).

## 1. T2 PITR drill — PASS (RTO ~10 min, plan ≤ 240)

- Baseline: legacy_slug_map=11, daily_report_extras=2, alert_instances=2.
- WAL switch → target 2026-09-12T13:37:57+00 → writers stopped (incl. EMQX) →
  marker `dr_marker_1789220314` written after the target → PGDATA wiped →
  `wal-g backup-fetch LATEST` → restore_command + recovery_target_time +
  recovery.signal → replay paused at target ("stopping before commit of
  transaction 15436, time 13:38:00" — correct: everything before target kept).
- Verify: marker count 0 (absent), parity 11/2/2 exact, hypertables
  `saloon_telemetry` + `metric_hourly_aggregates` present.
- Promote → full stack restart → API healthy. DR-LOG row 1.

## 2. T3 Redis-wipe drill — PASS on run 2 (RTO ~5 min, plan ≤ 15)

- Run 1 VOID: procedure green but the volume rm failed silently (a stopped
  container still holds its volume) — nothing was wiped. Script fixed
  (`rm -f redis` + fatal on rm failure). Lesson recorded, not hidden.
- Run 2, real wipe (volume destroyed + recreated, DBSIZE reset):
  - Leg 1 `rebuild_active_state_from_db`: clean on the new M8-03 code
    (0 breach / 0 active — ledger empty, honest result).
  - Leg 2 `cache_daily_thresholds`: 6/6 saloon hashes recached with full
    bands (temp/humidity min/max).
  - Leg 3 LOS window 10 min honored; no false-LOS wave observed.
- End-to-end after the wipe: HIL P2 live publish + P4 buffered replay both
  landed in the DB with correct timestamps. DR-LOG rows 2–3.

## 3. HIL sim self-test — 4 PASS + 2 honest SKIP

- `check_hil_readiness.py` live: 8/8 PASS (health, hardware/time, config
  route, O9 docs, T3–T9 wiring, OTA tooling, LOS tunable).
- `hil_harness.py --target sim`: P1 config 200, P2 telemetry honest,
  P3 LWT offline observed, P4 buffered replay original-ts; P5/P6 SKIP
  (no device to answer — correct in sim).
- Six harness bugs found live and fixed (tls arg, paho _sock + TLS-only
  connect, instant timestamp compare, stale-row poll, include_buffered,
  --hardware-id + --mqtt-observer-user/pass flags).

## 4. Drill-found product fixes (all committed)

- wal-g image rebuilt for Alpine (apk + gcompat + real asset filename).
- Custom `config_file` must repeat `listen_addresses` + timescaledb preload.
- Rehearsal postgres carries PGUSER/PGHOST libpq env for wal-g.
- Flock lookups join the admin-alias routing (M8-03b) — the midnight
  backstop was still a silent no-op without it.
- Drill legs run synchronously (`apply_async().get`) — fire-and-forget
  `celery call` hid a parked stub behind green output.
- Rehearsal app images rebuilt from tree; MinIO moved to quay.io.

## 5. NOT claimed

Real ESP32 gateway, power-cut replay on hardware, firmware-spec corrections
(M8-04/05/06 → `v0.8-HIL`, hardware-team owned). T2 script step-6 rewrite is
untested end-to-end (the live run used identical manual steps) — re-prove at
the next schema change.
