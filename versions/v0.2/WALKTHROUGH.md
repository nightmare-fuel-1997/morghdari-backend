# v0.2 WALKTHROUGH — how to run and demo it

Prereqs: v0.1 running (dev compose up, migrations applied).

## 1. Seed three users (management shell)

```bash
python manage.py shell
```

Create org "Golden Egg" + users `owner/ge`, `manager/ge`, `worker/ge` with scopes;
plus a platform-staff user `support` (`is_platform_staff=True`, not superuser).

## 2. Login and inspect the claims

```bash
curl -s -X POST localhost:8000/api/v1/auth/login/ -H 'Content-Type: application/json' \
  -d '{"username":"manager","password":"ge"}' | python -m json.tool
```

Decode the access token (jwt.io or `pyjwt`): observe `org_id`, `roles:["manager"]`.

## 3. Role enforcement

Call a manager-only surface as worker → expect **403**; same call as manager → passes.
Tamper with the token signature → **401**.

## 4. Refresh semantics (no rotation — owner decision 2026-08-23)

Refresh twice with the SAME refresh token: **both attempts succeed** (each returns a
fresh access token; there is no rotation and no blacklist). Then tamper with a token
signature → **401**. The revocation bound is the 15-min access lifetime only — say the
accepted risk out loud in the demo (Security §3.2).

## 5. Support session (the audit story)

```bash
# without incident_id → 400
curl -s -X POST localhost:8000/api/v1/platform/support/session/ \
  -H 'Authorization: Bearer <support-token>' -H 'Content-Type: application/json' \
  -d '{"org_id":"<uuid>"}'
# with incident_id → 200 + expires_in_sec:900
... -d '{"org_id":"<uuid>","incident_id":"INC-7","reason":"gateway offline"}'
```

Then show the accountability row:

```sql
SELECT action, details FROM account_activity_logs WHERE action='SUPPORT_ORG_ENTER';
-- {"incident_id": "INC-7", "reason": "gateway offline"}
```

Demo script (record): steps 2 → 3 → 5 in order; end on the audit query — it is the
tenant-trust payoff of the whole version.
