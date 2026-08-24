# v0.2 — Auth & Tenancy *(M2)*

> **Theme:** put identity and role enforcement on top of the RLS foundation.
> **Risk killed:** app-layer isolation and role enforcement.
> **Maps to:** Blueprint **M2** · **Duration:** ~1–2 weeks (+small for the support role)

## What this version delivers

- SimpleJWT authentication with **org/roles claims embedded in the token** (stateless
  tenant resolution — no session lookups)
- Tenant middleware that sets `app.current_org_id` from the validated claim via
  transaction-scoped `set_config(..., true)` — the HTTP twin of v0.1's `tenant_context()`
- RBAC permission classes (`require_role`) driven by `account_role_scopes`
- `/auth/login/`, `/auth/token/refresh/` **without rotation or blacklist** (owner
  decision 2026-08-23, Tracker §4i); org-member management endpoints
- **Platform Support Engineer role** + `POST /platform/support/session/`: audited
  org-context sessions (15-min scoped token, mandatory `incident_id`) — defined here,
  activated at v0.7 (Security §4.5)
- Gateway-request review permission reserved as platform-side (admin/support default,
  extensible) — the GS-BOARD decision's code home is v0.5

## Exit demo

Login as three roles → worker receives 403 on manager surfaces · forged/expired tokens
rejected · refresh with the same token twice still succeeds (no rotation — accepted
risk) · a support session without `incident_id` returns 400; with one it opens and
writes a `SUPPORT_ORG_ENTER` audit row.

## Files

[TASKS.md](TASKS.md) · [WALKTHROUGH.md](WALKTHROUGH.md) · [TUTORIAL.md](TUTORIAL.md)

## Look-back questions

Does the JWT org-claim model hold for installers (cross-farm scope)? Did GS-BOARD land
(platform-side review, extensible)? Staff off-boarding mid-session: does 15-min expiry
cover revocation?
