# v0.8 TASKS — ordered breakdown

> **Scope cut 2026-09-12 (backend-only close as `v0.8-DR`, owner go):** backend owner
> skips all ESP32/firmware hands-on work. M8-04/M8-05/M8-06 PARKED — hardware-team
> owned, tracked as `v0.8-HIL` follow-up; the receiver-side gate (`hil_harness.py
> --target real`, M8-04-H) stays delivered. v0.8-DR closes on M8-01/02/03 + M8-04-H
> + live T2/T3 DR-log rows. Full M8 exit demo (real radio) is NOT claimed.

| # | Task | Depends on | Acceptance | Status |
|---|---|---|---|---|
| M8-01 | wal-g in postgres image + archive_mode=on + archive_command; base backups scheduled | v0.7 | `wal-g backup-list` shows bases; WAL continuous | ✅ LIVE 2026-09-12 (rehearsal): wal-g v3.0.9 on rebuilt Alpine image, archive_mode=on, base `base_000000010000000000000008` pushed to MinIO, WAL segments flowing, timeline consistent |
| M8-02 | T2 drill on staging: full PITR to a target time; verify row counts + hypertables | M8-01 | first DR-log entry; RPO/RTO vs plan noted | ✅ LIVE 2026-09-12: target 13:37:57+00, marker absent, parity 11/2/2, hypertables present, RTO ~10 min (plan ≤ 240) — DR-LOG row 1 |
| M8-03 | T3 drill: catastrophic Redis wipe → rebuild task + cache warmers + LOS suppression | v0.4 | alerting fully rearmed within window | ✅ LIVE 2026-09-12: real volume wipe, rebuild clean on new code, 6/6 threshold hashes with full bands, HIL P2+P4 end-to-end after the wipe, RTO ~5 min (plan ≤ 15) — DR-LOG rows 2–3 (run 1 VOID: fake wipe) |
| M8-04 | HIL rig: ESP32+LoRa gateway firmware on real radio; one sensor pack | v0.5 config endpoint | real device zero-touches end-to-end | 🅿️ PARKED for `v0.8-DR` (backend-only close 2026-09-12 — no ESP32 hands-on; hardware-team owned as `v0.8-HIL`). Software side DELIVERED: `scripts/hil_handover/hil_harness.py --target real` is the executable gate (P1–P6) |
| M8-04-H | Handover harness + C5 readiness (receiver-side acceptance the hardware team must pass at bring-up) | M8-03, v0.5 | `hil_harness.py --target sim` green + `check_hil_readiness.py` green; `--target real` is the handover gate | ✅ implemented 2026-09-12 (`scripts/hil_handover/`, `scripts/check_hil_readiness.py`, `scripts/ota_checksums.py`) + **proven live 2026-09-12 on rehearsal**: readiness 8/8 PASS; harness `--target sim` 4 PASS (P1–P4) + 2 honest SKIP (P5/P6 need a real device); results `%TEMP%/opencode/hil_sim_results.json` |
| M8-05 | Power-cut test: cut mains to gateway; FIFO replay with ORIGINAL timestamps after restore | M8-04 | buffered_delay_mins reflects reality; no clock lies | 🅿️ PARKED for `v0.8-DR` (needs real gateway; hardware-team owned as `v0.8-HIL`). Backend data-path probed by harness P4 (`is_buffered` + ORIGINAL device_ts) |
| M8-06 | Firmware-spec correction notes swept into docs + Tracker | M8-04..05 | findings tracked with owners | 🅿️ PARKED for `v0.8-DR` (findings come from real radios; hardware-team owned as `v0.8-HIL`) |
| M8-07 | DoD sweep: CHANGELOG, tag `v0.8-DR`, demo recorded, DR log updated | M8-01..03 + M8-04-H (M8-04..06 parked) | tag pushed | ⏳ backend legs live-green (T2 + T3 + sim); left: CHANGELOG close-out, tag `v0.8-DR`, demo transcript |
