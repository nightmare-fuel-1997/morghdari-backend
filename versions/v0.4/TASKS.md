# v0.4 TASKS — ordered breakdown

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M4-01 | Threshold cache + midnight cron + flock-change signal re-cache | v0.3 | cache reflects flock-age day; end_date mid-day re-arms |
| M4-02 | Breach accumulation w/ TTL-refresh + hysteresis (+0.5) + midnight grace 300 s | M4-01 | `test_breach_key_survives_exactly_15_minutes`, `test_hysteresis_flapping_prevention` green |
| M4-03 | NH₃ velocity bypass (config key, max_ts ≥ 50 → instant Tier 1) | M4-02 | `test_velocity_bypass_duration_filter` green |
| M4-04 | alert_instances writes (REALTIME; current_value stamped; batch_id NULL) | M4-02 | rows carry last breach value |
| M4-05 | Escalation sweep (every 2 min; Tier 2 at 15 min, Tier 3 at 30) + 3-tier delivery (push/SMS stubs) | M4-04 | `test_threshold_breach_no_alert_below_duration` + `test_threshold_breach_triggers_alert` + tier timeline green |
| M4-06 | Ack MECHANISM: acknowledgement row + pause key (TTL from config) written in ONE transaction — the REST acknowledge endpoint arrives with the v0.6 contract surface (M6-01) | M4-05 | `test_alert_acknowledgement_pause` green |
| M4-07 | LOS watchdog (60 s sweep, 300 s grace) + Redis-restart suppression (config) | M4-01 | `test_los_watchdog_sweep_fires_after_ttl` + suppression green |
| M4-08 | Replay sweep (REPLAY_ANALYSIS, batch_id idempotent, never pushes) | M4-04 | `test_replay_analysis_alert` + `test_pending_not_pushed_to_ws_or_alerts` green |
| M4-09 | D23 capability-gated rules: THI (temp+humidity), co/power_* where live | M4-02 | `test_capability_profile` green |
| M4-10 | **GS-HUMAN-SAFETY REJECTED** (owner, 2026-08-23 — Tracker §4i): NO flock-independent baselines; alerting stays flock-gated (D10 velocity bypass unchanged). Replaces it: **GS-CORRELATION rule** (≥ `farm_power_outage_los_ratio` of a farm's slots LOS within `farm_power_outage_window_min` → ONE consolidated notification + `correlation_suppress_minutes` paging suppression) + **maintenance-mute windows** (`maintenance_mutes`; suppress threshold/duration alerts ONLY, never velocity; digest audit line) | M4-07 | `test_farm_power_outage_correlation` + `test_maintenance_mute_window` green |
| M4-11 | Coverage ≥90 % + MET-ALERT-QUALITY metrics (MTTR/tier, ack-to-resolve, alerts-per-flock vs mortality — Observability §5.2) recorded for retrospective | all | suite green; numbers in CHANGELOG |
| M4-12 | DoD sweep: CHANGELOG, tag `v0.4`, demo recorded, docs swept | all | tag pushed |
