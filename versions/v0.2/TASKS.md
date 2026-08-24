# v0.2 TASKS — ordered breakdown

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M2-01 | Install SimpleJWT; settings block (15-min access / 7-day refresh; **rotation OFF, blacklist OFF** — owner decision 2026-08-23, Tracker §4i) | v0.1 | token pair issued on login |
| M2-02 | Custom `TokenObtainPairSerializer` embedding `org_id`, `roles[]`, `is_platform_staff` | M2-01 | decoded claims visible in test |
| M2-03 | `/auth/login/` + `/auth/token/refresh/` endpoints per OpenAPI schemas | M2-01 | contract-shaped 200/401 |
| M2-04 | No-rotation semantics verified | M2-03 | refresh with the SAME token twice → both return fresh access tokens (no rotation, no blacklist rows) |
| M2-05 | TenantMiddleware: GUC from validated claim (transaction-scoped); platform-admin path separate | M2-02, v0.1 tenant_context | request-scoped isolation proven in tests |
| M2-06 | RBAC: `require_role` permission classes + `account_role_scopes` farm/saloon narrowing | M2-05 | worker→manager surface = 403 |
| M2-07 | Org-member management endpoints (invite/list/deactivate staff) | M2-06 | owner manages; manager cannot |
| M2-08 | Support role + `/platform/support/session/` (400 without incident_id · 200 + 900s scoped token · SUPPORT_ORG_ENTER audit row) | M2-02 | per Security §4.5 |
| M2-09 | Token revocation story: offboarded member's ACCESS dies ≤15 min; refresh token stays valid until its natural 7-day expiry (no blacklist — accepted risk, Security §3.2) | M2-04 | test proves the access-token bound; risk documented |
| M2-10 | RBAC matrix test suite (role × endpoint parametrized) | M2-06..08 | full matrix green |
| M2-11 | DoD sweep: CHANGELOG, tag `v0.2`, demo recorded (WALKTHROUGH script), docs swept | all | tag pushed |
