# v0.9 TASKS — ordered breakdown

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M9-01 | Pilot farm selected + agreement; gateway mounted on real saloon wall (radio survey) | v0.8 | device live, RSSI acceptable at far corner |
| M9-02 | Shadow-run: 2 weeks parallel with existing practice; NO reliance by the farm yet | M9-01 | daily reconciliation report generated each day | Tooling ✅ done 2026-09-12 (`GET /reconciliation/` + `recon_day` command, live-proven on rehearsal); field loop ⏳ parked (no farm) |
| M9-03 | On-call shadowing: real pages answered per rota; every page post-mortemed same-day | M9-01 | zero unhandled criticals |
| M9-04 | Status page live (status.morghdari.com, uptime history) | v0.7 prober | public URL green 14 days (MET-STATUS) | Backend ✅ done 2026-09-12 (public `/status/` JSON + browsable HTML from blackbox history, min-headline, unknown-never-500, contract 1.7.7, live-proven on rehearsal; 14-day green + domain stay field-side) |
| M9-05 | Farm-staff real-alert drill: one genuine alert handled end-to-end BY staff | M9-03 | staff handled it; feedback recorded |
| M9-06 | Full retrospective → rewrite v1.0 checklist; sweep findings to Tracker | M9-02..05 | checklist committed |
| M9-07 | DoD sweep: CHANGELOG, tag `v0.9`, demo = the 14-day log itself | all | tag pushed |

## Scope additions (from canonical `VERSIONS.md` §v0.9 — no reorder, no new spiral)

| # | Scope | Acceptance |
|---|---|---|
| M9-08 | Granular panels (Tracker §4n, 2026-09-04): `panel_definitions` + role/user overrides, `reviewer` read-only, default-open, owner admin panel | role/user overrides hold; reviewer cannot approve | ✅ done 2026-09-12 (8-panel seed catalog, D24-2 chain with deny-wins + absolute owner panel, owner-only writes, contract 1.7.6, alerts/0007 + initdb RLS row; visibility-only re-cut documented) |
| M9-09 | Daily reports lifecycle (Tracker §4n): `daily_reports` DRAFT→SUBMITTED→APPROVED/REJECTED + FLAGGED, Tehran-day time-lock, vet catalog (`vet_medication_catalog`, vet-only writes), alert subscriptions (`alert_subscriptions`, Tier-1 velocity never muted) | same-day edit only; NH3 velocity always pages | Reports + catalog: ✅ shipped v0.7. Subs: ✅ done 2026-09-12 (org-wide mute table + APIs + engine lane ②c, velocity structurally unmutable in-suite, contract 1.7.5, alerts/0006 + initdb RLS row) |
| M9-10 | Aggregates batch 1 (Tracker §4p): four daily tables + nightly writers + exact backfill (`metric_daily_aggregates`, `flock_daily_snapshots`, `alert_daily_stats`, `gateway_daily_uptime`) | backfill exact; nightly job green | ✅ done 2026-09-12 (unmanaged models + monitoring/0002 DDL/RLS, 4 idempotent writers reusing KPI math, beat 00:30–01:00 Tehran, backfill command, 7-test module green on sqlite + PG; read-path fallback rewiring explicitly deferred) |

## Hardware track (locked 2026-09-12, backend-only close — the Hardware Team actor had zero system presence)

| # | Task | Depends on | Acceptance | Status |
|---|---|---|---|---|
| H0 | Gateway HW register + retire (`POST /platform/support/hardware/gateways/`, retire path), platform-staff session only, audited | v0.8 | serial+slot linked without SQL; sync passes right after; 409 on occupied; contract 1.7.3 | ✅ done 2026-09-12 (`apps/accounts/api_hardware.py`, 13-test module, yaml 1.7.3) |
| H1 | Sensor HW register + retire (same pattern, `HardwareRegistry`) | H0 | same semantics; unknown unit → 400 downstream, never silent | ✅ done 2026-09-12 (same module/tests/contract as H0) |
| H2 | Actuator HW register + retire (same pattern, `ActuatorHardwareRegistry`) | H0 | same-shape, covered by the H-track test module | ✅ done 2026-09-12 (same module/tests/contract as H0) |
| H3 | Gateway replacement ceremony: retire old unit + fresh one-time creds + revoke old EMQX authn user + slot/ACL continuity | H0 | old password dead at broker, new connects, no gap beyond the swap | ✅ done 2026-09-12 (replace endpoint, one-time password never re-issued, FNV-soft broker push, contract 1.7.4) |
| H4 | Downlink ACL provisioning (T5–T9 ota/commands rules) for real gateways | H0 | HIL `--target real` P5/P6 have rules (today: no-match deny) | ✅ done 2026-09-12 (acl-backfill endpoint, additive-only, re-runs add zero, contract 1.7.4) |
| H5 | Verify-only: platform staff reach O9 diagnostics + LOS alerts via support session | v0.7 | checklist green, no code expected | ⏳ with B-wave verification |
