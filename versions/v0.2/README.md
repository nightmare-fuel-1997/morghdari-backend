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

### Answers (recorded at close, 2026-08-25)

1. **Installers (cross-farm scope):** YES. The token carries the union of
   `org_members.role` ∪ `account_role_scopes.role`; cross-farm reach comes from
   PROVINCE-scoped scope rows narrowing querysets via
   `apps/accounts/permissions.py::scoped_farms/scoped_saloons` (M2-06 truth table).
2. **GS-BOARD:** NOT in v0.2 by design — platform-side review permission is reserved
   (support role exists); its code home is v0.5 per the GS-BOARD decision.
3. **Staff offboarding mid-session:** YES for tenant authority — live access tokens die
   ≤15 min (proven: exp−iat==900), and the next refresh RE-DERIVES claims, yielding an
   org-less token whose reads are HTTP 200 + empty (C-14). The refresh token itself stays
   valid to natural 7-day expiry — the documented accepted risk (Security §3.2) — but it
   confers NO tenant data authority anymore.
