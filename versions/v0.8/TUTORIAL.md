# v0.8 TUTORIAL — survive disaster and real hardware yourself

> ⚠️ **Pre-spiral draft** — re-validate at spiral start. **How to use:** 1–4 teach,
> 5 is hands-on, 6 warns, 7 checks. KB lesson: *Infrastructure & Ops* (#/infra).
> Canonical: DR Plan §3–4, Gateway Firmware Specification.

---

## 1. The Mission

Prove the two claims every IoT vendor makes but rarely tests: "we can recover from
anything" and "it works with real hardware." Rehearse catastrophe on staging until
boring, then attach a real ESP32+LoRa gateway and let physics review your design.

**Done means:** PITR drill logged within RTO · Redis-wipe drill recovers alerting in
window · real gateway zero-touches, streams, survives a power cut with FIFO replay.

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| wal-g + `archive_mode=on` | Continuous WAL archiving → true PITR | T2's RPO (1 h) is impossible with dumps alone | Not a replacement for nightly dumps — both |
| `recovery_target_time` + `recovery.signal` | Replay WAL to a chosen instant | Point-in-time recovery mechanics | Target beyond archived WAL = recovery hangs |
| AOF replay + rebuild tasks | Redis disaster path | Alert state must re-arm, not just data | Restart alone ≠ recovery — caches need warmers |
| ESP32 + LoRa gateway (real) | Actual radio hardware in the loop | Simulators can't model walls or power cuts | Never test against production EMQX creds |
| FIFO buffer + original timestamps | Gateway outage buffering | Audit trail needs device truth | Not infinite — depth has limits (firmware spec) |

---

## 3. The Build

### 3.1 Bake wal-g into postgres

Image build step installs wal-g; postgres.conf gains:

```
archive_mode = on
archive_command = 'wal-g wal-push %p'
archive_timeout = 300        # ≤5 min WAL exposure = T2's RPO math
```

Nightly `wal-g backup-push` + one before every deploy. Verify weekly:
`wal-g backup-list` shows fresh bases AND recent WAL segments.

### 3.2 The T2 drill — annotated transcript

```bash
# 1. stop EVERYTHING touching the DB — including emqx (open connections block DROP)
docker compose stop daphne celery-worker celery-beat subscriber-01 subscriber-02 emqx
# 2. restore base backup into a clean data dir, then point recovery at your moment:
cat >> postgresql.auto.conf <<EOF
restore_command = 'wal-g wal-fetch %f %p'
recovery_target_time = '2026-XX-XX 14:37:00+00'   # a VERIFIED mark, not a guess
EOF
touch recovery.signal                              # the switch that starts replay
docker compose up -d postgres                      # replays WAL up to target
# 3. verify: the test row written AFTER target must be absent
# 4. promote: SELECT pg_wal_replay_pause(); SELECT pg_promote();
# 5. restart services; record elapsed vs RTO 4h
```

Common stall: target time ahead of archived WAL → recovery waits forever. Pick marks
you can verify from the archive list.

### 3.3 The T3 drill — four steps everyone forgets three of

Wipe volume → restart (AOF replay if intact) OR clean volume + recreate. Then, IN
ORDER: (1) `alerts.rebuild_active_state_from_db` restores breach/active keys;
(2) `alerts.cache_daily_thresholds` + zone/status cache warmers — WITHOUT these,
alerting stays silently OFF for up to 24 h; (3) LOS suppression window honored
(`los_suppress_after_redis_restart_min`); (4) inject a breach and WATCH it fire.
Time everything against RTO 15 min.

### 3.4 HIL — the radio doesn't read specs

Flash the firmware per spec; mount at a REAL saloon wall. Watch, in order:
zero-touch config fetch over the actual internet → TLS MQTT publish → backend resolves
packs. Then walk the sensor pack to the far corner (RSSI survey), open/close metal
doors near it, run a heater alongside it. Every deviation from simulation becomes a
doc correction or Tracker item — that IS the deliverable.

### 3.5 Power-cut honesty

Cut mains (not wifi): gateway buffers to FIFO, dies, returns on power → replays with
ORIGINAL device_ts. Verify `buffered_delay_mins` tells the truth, `clock_drift` flags
if the RTC drifted, and alert evaluation catches up without spamming.

---

## 4. Cheat Sheet

| Drill | First command | Success evidence | RTO/RPO check |
|---|---|---|---|
| T2 PITR | `wal-g backup-list` | test-row absent; counts match | elapsed ≤ 4 h; loss ≤ 1 h |
| T3 Redis | wipe volume → `up -d` | injected breach FIRES after rebuild | ≤ 15 min |
| Power cut | cut mains | replay rows w/ old device_ts | delay mins truthful |
| HIL zero-touch | power the gateway | config fetch 200 in logs | — |

---

## 5. Your Subtasks

1. **Install wal-g + archive_mode; force a WAL switch; confirm upload.** Verify: archive
   listing grows.
2. **Take a base backup; write a marker row; note the time.** Verify: marker exists now.
3. **Execute T2 to just BEFORE the marker.** Verify: marker gone, prior data intact,
   hypertables listed. Notice: extension pre-create or pg_restore chokes.
4. **Run the T3 sequence with a stopwatch.** Verify: breach fires after rebuild within
   window. Notice: which of the four steps you forgot first — everyone forgets warmers.
5. **Flash + mount the HIL gateway; watch zero-touch succeed on real radio.**
   Notice: what the sim never showed you.
6. **Cut power 30 minutes; restore.** Verify: replay preserves original device_ts;
   buffered_delay truthful.
7. **Sweep findings into docs + Tracker.** Verify: each surprise owned. Notice: this
   feedback loop is why HIL exists.
8. **DoD sweep: CHANGELOG, tag `v0.8`, DR log updated.**

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| emqx left running during DROP DATABASE | Restore blocked by open connections |
| Recovery target after last archived WAL | Infinite wait, no error |
| T3 without cache warmers | Alerting silently dead for up to 24 h |
| HIL against prod credentials | Real farms receive test storms |
| Trusting wall-clock during power cycles | device_ts lies; audit corrupted |

---

## 7. Prove It

1. Why does T2 require stopping EMQX when T3 does not? *(§3.2/§3.3.)*
2. Name the artifact that makes point-in-time possible and the file that triggers its
   use. *(§3.2.)*
3. Which T3 step prevents a fleet-wide false LOS page, and where is its duration set?
   *(§3.3/config.)*
4. Give ONE concrete example of a finding only real radios reveal. *(§3.4 — use YOUR
   drill's finding.)*
5. What would be lost forever if the gateway died mid-outage WITHOUT FIFO? How large is
   that risk per the firmware spec? *(§3.5.)*
