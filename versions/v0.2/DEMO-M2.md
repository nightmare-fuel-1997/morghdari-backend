# v0.2 EXIT DEMO — auth & tenancy (recorded 2026-08-25)

> EN: Scripted transcript per WALKTHROUGH steps 2 → 3 → 5, ending on the
> audit query — the tenant-trust payoff of the version. All outputs below are
> the exact shapes asserted by `backend/tests/test_m2_0{3,4,7,8,9}_*.py` and
> the RBAC matrix (`test_m2_10_rbac_matrix.py`, 18 verdicts).
>
> فارسی: رونوشتِ دموی خروج نسخهٔ ۰٫۲ — از ورود سه نقش تا ردیفِ ممیزِ
> SUPPORT_ORG_ENTER که پایانِ دمو است.

## Step 2 — login & claims (three identities)

```bash
curl -s -X POST localhost:8000/api/v1/auth/login/ -H 'Content-Type: application/json' \
  -d '{"username":"mx-owner","password":"<pw>"}'
```
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "user": {"id": 3, "username": "mx-owner", "roles": ["owner"],
           "org_id": "<uuid>", "org_name": "matrix-org",
           "is_platform_staff": false}
}
```

Staff login is ORG-LESS by design (D15): same call with a platform-staff
account returns `"roles": []`, `"org_id": null, "org_name": null`,
`"is_platform_staff": true`.

## Step 3 — no rotation, tamper rejection

Refresh with the SAME token twice → both `200` and each returns a fresh
`{"access": "..."}`. Tamper one character → `401`.
(The accepted risk, said out loud: a leaked refresh is usable to its natural
7-day expiry — Security §3.2.)

## Fail-closed proof (middleware branches)

- Platform staff WITHOUT a support session calling any tenant read:
  HTTP **200** with an EMPTY payload — branch ② sets NO GUC; FORCE RLS shows
  zero rows. Not a backdoor.
- Deactivated member (C-14 chain): owner deactivates via
  `POST /org/members/{id}/deactivate/` → victim's still-valid refresh returns
  an access token WITHOUT `org_id` → their next tenant read is HTTP **200**
  with `[]`. Live tokens died within ≤15 min.

## Step 5 — support session (the audit story)

```bash
# without incident_id → 400
curl -s -X POST localhost:8000/api/v1/platform/support/session/ \
  -H "Authorization: Bearer <staff-access-token>" -H 'Content-Type: application/json' \
  -d '{"org_id":"<uuid-b>"}'
# → {"error": "incident_id required"} 400

# with incident_id → 200 + scoped token
curl -s -X POST localhost:8000/api/v1/platform/support/session/ \
  -H "Authorization: Bearer <staff-access-token>" -H 'Content-Type: application/json' \
  -d '{"org_id":"<uuid-b>","incident_id":"INC-7","reason":"gateway offline"}'
```
```json
{"access_token": "eyJ...", "org_id": "<uuid-b>",
 "incident_id": "INC-7", "expires_in_sec": 900}
```

Using THAT token, org B's data becomes visible to the staff engineer under
ordinary RLS (branch ① beats the staff skip) — and ONLY org B's.

The accountability row:

```sql
SELECT action, details FROM account_activity_logs WHERE action='SUPPORT_ORG_ENTER';
--      action       |                    details
-- ------------------+-------------------------------------------------
--  SUPPORT_ORG_ENTER | {"incident_id": "INC-7", "reason": "gateway offline"}
```

## Gates at close

- Isolated CI gate: `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build test`
  → **91 passed @ 87.25%** on timescale/timescaledb:2.29.2-pg16
- Tag: `v0.2` (annotated)
