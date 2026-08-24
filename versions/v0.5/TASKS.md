# v0.5 TASKS — ordered breakdown

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M5-01 | Status-transition service (atomic: status + audit row + cache delete) used everywhere | v0.2 | no direct .save() on provisioning_status outside service |
| M5-02 | Gateway-request flow: submit (manager+, cap from config) / list / review endpoint (platform permission: admin+support default, extensible) | M5-01, v0.2 | approval creates slot+credentials; cap 400s at limit |
| M5-03 | Provisioning sync endpoint (installer-scoped): zones, mappings (hardware derived server-side), replacements → PENDING_APPROVAL | M5-01 | R6 contract tests green |
| M5-04 | Subscriber status branch: PENDING_APPROVAL → pending buffer; REJECTED/SUSPENDED discarded/archived | M3-04, M5-01 | pre-approval telemetry held (R3 suite) |
| M5-05 | FM approve/reject endpoints (+audit, cache invalidation, drain dispatch) | M5-01, M5-04 | approve drains in order; reject discards |
| M5-06 | `GET /gateways/{id}/config/`: Basic-auth vs bcrypt creds, rate-limit note for nginx, R2 payload (org/farm ids, no slot_ids) | M5-01 | config fetch works pre/post approval; 403 on rejected/suspended |
| M5-07 | EMQX ACL wiring from mqtt_acl_rules (allow-rows, no_match deny) + suspension revocation | M5-01 | suspended gateway cannot publish |
| M5-08 | Staged rotation: ROTATE_CREDENTIAL command, pending hash, commit-on-ACK, 24h-pending alert | M5-06, v0.3 commands? (stub ok) | rotation dance end-to-end with simulator |
| M5-09 | Concurrency test: two simultaneous syncs for the same gateway → per-slot Redis lock; second gets **409 + Retry-After**, zero partial topology (AU-RACES closed 2026-08-23, Blueprint §6); transitions checked against the Blueprint §11 state matrices (AU-STATES closed) | M5-03 | `test_sync_conflict_409` green; no partial writes |
| M5-10 | DoD sweep: CHANGELOG, tag `v0.5`, demo recorded (WALKTHROUGH full sequence), docs swept | all | tag pushed |
