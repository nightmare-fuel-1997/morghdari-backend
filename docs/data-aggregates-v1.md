# Data Aggregates — Rollup Tables Spec (Batch 1 → v0.9 pilot, Batch 2 → v1.2)

> **Status:** DRAFT 2026-09-05 · **Additive-only** — six small new tables + nightly Celery
> writers; no `ALTER` on core tables, no change to existing read paths (KPI/threshold writers
> keep working; aggregates are read-accelerators with compute-fallback, §5).
> **Canonical homes:** this file (table + writer contracts) · decisions: `Open Items Tracker.md`
> §4p · plan: `VERSIONS.md` §§v0.9/v1.2 · history: `CHANGELOG.md [Planned → 1.7.0/1.8.0]`.
> Current state: the ONLY rollup is `metric_hourly_aggregates`; raw `saloon_telemetry` is kept
> 180 d uncompressed. Consumers today: KPI endpoint (per-request over full history),
> compliance % (per-request over hourly rows), MET-ALERT-QUALITY (exportable function only).

---

## 1. Retention tiering (locked rule)

| Tier | Content | Retention | Serves |
|---|---|---|---|
| Raw | `saloon_telemetry` (1-min windows) | 180 d (unchanged) | gap-fill, forensics, replay sweep |
| Hourly | `metric_hourly_aggregates` (exists) | permanent (unchanged) | charts, threshold cache seeds |
| **Daily (new)** | §2 tables | **permanent** (rows are tiny: ~6 tables × orgs × saloons × days) | dashboards, KPIs, SLA, billing metering |

Capacity note: daily rows are ~3 orders of magnitude fewer than raw; the AU-DISK-MATH model
(Deployment Guide §11) is unaffected — re-run its arithmetic at implementation and record the
delta in the release draft.

## 2. Batch 1 — pilot numbers (vehicle: v0.9; the pilot is judged by numbers, §4p AG-01)

All writers: nightly Celery beat (Asia/Tehran, after the midnight threshold cron), idempotent
upserts on `(org_id, day, …)`, Tehran-calendar `day DATE`. All tables carry `org_id UUID NOT
NULL` + RLS like every tenant table.

| Table | Grain (PK/UNIQUE) | Key columns | Feeds |
|---|---|---|---|
| `metric_daily_aggregates` | `UNIQUE(org_id, saloon_id, slot_id, metric_key, day)` | `avg/min/max NUMERIC`, `inband_minutes INT` (minutes inside the day's threshold band — the compliance numerator), `sample_count INT` | compliance % becomes O(days); chart zoom-out |
| `flock_daily_snapshots` | `UNIQUE(flock_id, saloon_id, day)` | `mortality_cum INT`, `mortality_pct NUMERIC`, `feed_cum_kg NUMERIC`, `water_cum_l NUMERIC`, `avg_weight_g NUMERIC NULL`, `fcr_to_date NUMERIC NULL` (NULL until weight+feed both exist — D22 NULL-rule) | KPI endpoint fast path + trend charts; `cycle_summary` reads the latest snapshot instead of full history |
| `alert_daily_stats` | `UNIQUE(org_id, saloon_id, day, tier, metric_key)` | `alert_count INT`, `mttr_min NUMERIC NULL`, `ack_to_resolve_min NUMERIC NULL` | MET-ALERT-QUALITY retrospective with numbers, not vibes |
| `gateway_daily_uptime` | `UNIQUE(gateway_slot_id, org_id, day)` | `online_pct NUMERIC`, `los_minutes INT`, `buffered_pct NUMERIC` (from telemetry presence + LWT/status topics) | first provable uptime story; backs the 99.9 % pilot gate |

## 3. Batch 2 — SaaS numbers (vehicle: v1.1 metering, v1.2 analytics)

| Table | Grain | Key columns | Feeds |
|---|---|---|---|
| `org_daily_usage` | `UNIQUE(org_id, day)` | `msg_count BIGINT`, `stored_bytes BIGINT`, `saloon_count INT`, `gateway_count INT` | cost-per-tenant (MET-SAAS) + billing placeholder + quota calibration. No endpoint in v1.8.0 — internal only |
| `notification_daily_stats` | `UNIQUE(org_id, day, category)` | `sent INT`, `suppressed_velocity_bypass INT`, `suppressed_mute INT`, `muted_digest_lines INT` | the "N alerts suppressed" UI + routing tuning (GS-ROUTING follow-up) |
| `analytic_insights` | per v1.2 plan (already in `VERSIONS.md` §v1.2) | computed on daily tables, no warehouse | lightweight analytics |

`analytic_insights` stays owned by the v1.2 plan; this file only guarantees its inputs exist.

## 4. Writer contracts (new Celery tasks, all idempotent)

| Task | Reads | Writes | Cadence |
|---|---|---|---|
| `aggregates.build_metric_daily` | `metric_hourly_aggregates` + day's `lifecycle_threshold_rules` | `metric_daily_aggregates` (upsert) | nightly 00:30 Asia/Tehran |
| `aggregates.build_flock_snapshots` | `mortality_records` + `flock_feed/water_intake` + `flock_weight_samples` + `flock_cycles` | `flock_daily_snapshots` (upsert) | nightly 00:40 |
| `aggregates.build_alert_stats` | `alert_instances` + `alert_acknowledgements` | `alert_daily_stats` (upsert) | nightly 00:50 |
| `aggregates.build_gateway_uptime` | `saloon_telemetry` presence + gateway status/LWT | `gateway_daily_uptime` (upsert) | nightly 01:00 |
| `aggregates.build_org_usage` | stream counters + `pg_total_relation_size` samples | `org_daily_usage` (upsert) | nightly 01:10 |
| `aggregates.build_notification_stats` | `notification_deliveries` + mute digest audit lines | `notification_daily_stats` (upsert) | nightly 01:20 |

Backfill: one-shot job recomputes history from existing hourly/raw rows (all source data is
retained, so day-one backfill is exact, not estimated). Late-arriving buffered telemetry
re-opens the affected day rows (upsert, never append-duplicate).

## 5. Read migration rule (no flag day)

KPI/compliance/alert-quality readers try the daily tables first and fall back to current
per-request computation when a day row is missing (backfill gap, brand-new saloon). Responses
stay byte-compatible; only latency changes. Remove fallbacks only by a written Tracker decision.

## 6. Acceptance checklist (docs-side)

- [ ] Six `CREATE TABLE` statements reviewed (all `org_id` + RLS + `UNIQUE` day grain).
- [ ] Backfill exactness proven on staging (snapshot KPIs == computed KPIs for 3 sample flocks).
- [ ] Late-buffered replay re-opens day rows (test: 12 h outage replay → snapshots corrected).
- [ ] Capacity-model delta recorded (`docs/releases/v1.7-ops-draft.md` §capacity or v0.9 record).
