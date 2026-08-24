# v0.8 TASKS — ordered breakdown

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M8-01 | wal-g in postgres image + archive_mode=on + archive_command; base backups scheduled | v0.7 | `wal-g backup-list` shows bases; WAL continuous |
| M8-02 | T2 drill on staging: full PITR to a target time; verify row counts + hypertables | M8-01 | first DR-log entry; RPO/RTO vs plan noted |
| M8-03 | T3 drill: catastrophic Redis wipe → AOF replay path + rebuild task + cache warmers + LOS suppression | v0.4 | alerting fully rearmed within window |
| M8-04 | HIL rig: ESP32+LoRa gateway firmware on real radio; one sensor pack | v0.5 config endpoint | real device zero-touches end-to-end |
| M8-05 | Power-cut test: cut mains to gateway; FIFO replay with ORIGINAL timestamps after restore | M8-04 | buffered_delay_mins reflects reality; no clock lies |
| M8-06 | Firmware-spec correction notes swept into docs + Tracker | M8-04..05 | findings tracked with owners |
| M8-07 | DoD sweep: CHANGELOG, tag `v0.8`, demo recorded, DR log updated | all | tag pushed |
