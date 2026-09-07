# v0.9 TASKS — ordered breakdown

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M9-01 | Pilot farm selected + agreement; gateway mounted on real saloon wall (radio survey) | v0.8 | device live, RSSI acceptable at far corner |
| M9-02 | Shadow-run: 2 weeks parallel with existing practice; NO reliance by the farm yet | M9-01 | daily reconciliation report generated each day |
| M9-03 | On-call shadowing: real pages answered per rota; every page post-mortemed same-day | M9-01 | zero unhandled criticals |
| M9-04 | Status page live (status.morghdari.com, uptime history) | v0.7 prober | public URL green 14 days (MET-STATUS) |
| M9-05 | Farm-staff real-alert drill: one genuine alert handled end-to-end BY staff | M9-03 | staff handled it; feedback recorded |
| M9-06 | Full retrospective → rewrite v1.0 checklist; sweep findings to Tracker | M9-02..05 | checklist committed |
| M9-07 | DoD sweep: CHANGELOG, tag `v0.9`, demo = the 14-day log itself | all | tag pushed |

## Scope additions (from canonical `VERSIONS.md` §v0.9 — no reorder, no new spiral)

| # | Scope | Acceptance |
|---|---|---|
| M9-08 | Granular panels (Tracker §4n, 2026-09-04): `panel_definitions` + role/user overrides, `reviewer` read-only, default-open, owner admin panel | role/user overrides hold; reviewer cannot approve |
| M9-09 | Daily reports lifecycle (Tracker §4n): `daily_reports` DRAFT→SUBMITTED→APPROVED/REJECTED + FLAGGED, Tehran-day time-lock, vet catalog (`vet_medication_catalog`, vet-only writes), alert subscriptions (`alert_subscriptions`, Tier-1 velocity never muted) | same-day edit only; NH3 velocity always pages |
| M9-10 | Aggregates batch 1 (Tracker §4p): four daily tables + nightly writers + exact backfill (`metric_daily_aggregates`, `flock_daily_snapshots`, `alert_daily_stats`, `gateway_daily_uptime`) | backfill exact; nightly job green |
