# v0.6 TASKS — ordered breakdown

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M6-01 | v1.1 operability endpoint set complete (saloons detail/telemetry/alerts/analytics, farms overview, provisioning reads/sync/approve/reject, ack — the REST acknowledge endpoint is BUILT HERE; v0.4 shipped only the pause mechanism — state, flocks incl. `placement_count` body (contract v1.4.2), gateway-requests+review, metric-types, config, health extended) | v0.5 | OpenAPI diff green for the set |
| M6-02 | v1.3 KPI/dashboard endpoints (weight-samples, feed-intake POST/GET, kpis, me/dashboard-config, dashboard-config/{role}) | M6-01 | diff green; D22 tests green |
| M6-03 | v1.4 water/biosecurity/capabilities endpoints | M6-01 | diff green; capability gating tests green |
| M6-04 | KPI computation service (D22 math incl. water:feed ratio) | M6-02 | §2.7 suites green |
| M6-05 | Capability profile service (FEATURE_MATRIX from D23) | M6-03 | unavailable→available demo works |
| M6-06 | Channels consumer: group push (`saloon_{org_id}_{saloon_id}`), ws:active cap, close codes, TTL refresh | v0.3 | two browsers see same update; cap closes **4000** (4003 = auth/org) |
| M6-07 | Reconnect gap-fill merge behavior (client `since`) | M6-06 | gap-fill test green |
| M6-08 | OpenAPI diff test in CI (`implemented == contract`, v1.4.2 file; gate scoped to the 32 non-platform paths — `/platform/support/*` lands at v0.7) | M6-01..03 | CI gate active |
| M6-09 | DoD sweep: CHANGELOG, tag `v0.6`, demo recorded, FE-SPEC inputs noted | all | tag pushed |
