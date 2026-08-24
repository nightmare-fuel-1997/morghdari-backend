# v1.0 TUTORIAL — onboard the first paying tenant yourself

> ⚠️ **Pre-spiral draft** — a RUNBOOK tutorial; its final content comes from the v0.9
> retrospective. **How to use:** 1–4 teach, 5 is hands-on, 6 warns, 7 checks.

---

## 1. The Mission

Onboard a real, paying organization start-to-finish **by following your own written
runbook** — no heroics, no memory, no improvisation. GA is not "it works"; it's
"someone else can repeat it from paper."

**Done means:** tenant live with their first flock under alerting · every deviation
from the runbook became either a runbook fix or a Tracker item · tag `v1.0` pushed.

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| Onboarding Runbook v1 | The numbered procedure (below) | Repeatability is the product of this version | Not a substitute for the v0.9-rewritten checklist |
| `tier_quota_mapping` row | Tenant's limits (farms/saloons/messages) | Enforcement reads tiers, not conversations | Don't hand-raise limits without recording tier |
| Support rota | Who answers, when | Paying customers deserve named humans | Not "call my cell" — write the rota |
| Cost-per-tenant panel | Marginal cost truth (MET-SAAS) | Pricing sanity since v0.7 | Not shown to tenant — internal |

---

## 3. The Build — Onboarding Runbook v1 (annotated)

```text
STEP 0  Contract signed; tier chosen.
EXECUTE: `docker compose exec postgres psql -U morghdari_admin -d morghdari           -c "SELECT * FROM tier_quota_mapping WHERE tier='basic';"`
VERIFY: one row back (max_farms=5, max_gateways=2). No row → seed first; everything
        downstream silently misbehaves.
WHY: enforcement reads this row; missing = silent unlimited or silent block later.

STEP 1  Create organization + owner user (invite flow).
EXECUTE: `python manage.py onboard_org --name "<Tenant>" --tier basic           --owner-email owner@tenant.com`   (management command ships with M10-01)
VERIFY: decode the returned token: `org_id` + `roles:["owner"]` visible. (The REST
        contract has NO farm-list endpoint — RLS-emptiness is proven at STEP 3, where
        the owner's overview works and another org's token cannot see the farm.)
NOTICE: first human experience of the isolation you built at v0.1.

STEP 2  Quota seed check.
EXECUTE: trigger the quota sync (management command or beat task), then
         `redis-cli GET quota:org:<org_id>:farms` (or equivalent counter key)
VERIFY: counter equals the tier's max_farms. Notice: D21/D22 — limits come from data;
        a missed seed = invisible ceiling errors.

STEP 3  Farm + saloons created for the owner (ops-assisted in v1 — the REST contract
        has NO farm/saloon CRUD endpoints; self-service is a tracked gap,
        Tracker §6 "farm-saloon-crud-endpoints").
EXECUTE: `python manage.py onboard_farm --org "<Tenant>" --name "Home Farm"           --province 8 --city 91 --saloons 4`   (management command ships with M10-01)
VERIFY: `GET /api/v1/farms/1/overview/` as the OWNER token lists saloons each with
        gateway_status: null (the honest NO_GATEWAY state); the same call with a
        DIFFERENT org's token → farm not found (RLS proof #2).

STEP 4  FM submits gateway request → platform review → APPROVED.
EXECUTE: `curl -X POST .../gateway-requests/ -d '{"farm_id":1,"quantity":1}'` (FM token)
         `curl -X POST .../gateway-requests/1/review/ -d '{"decision":"APPROVED"}'`
         (platform token)
VERIFY: `SELECT slot_id, provisioning_status FROM gateway_slots;` → PENDING_INSTALLATION
        `SELECT count(*) FROM mqtt_device_credentials;` → ≥1 (hash stored, never plain)

STEP 5  Installer mounts + syncs → PENDING_APPROVAL.
EXECUTE: `curl -X POST .../provisioning/sync/ -d '{"gateway_slot_id":"GW-001",
          "saloon_assignments":[1],"sensor_slot_mappings":[{"pack_id":"P-A",
          "slot_id":"S01-PACK-A","position_id":1}]}'` (installer token)
VERIFY: `XLEN telemetry:pending_approval` grows while saloon_telemetry does not;
        status row reads PENDING_APPROVAL.

STEP 6  FM approves → PROVISIONED → drain replays buffer in order.
EXECUTE: `curl -X POST .../provisioning/gateways/GW-001/approve/ -H 'Auth...' -d '{}'`
VERIFY: `SELECT provisioning_status FROM gateway_slots WHERE slot_id='GW-001';` → PROVISIONED
        `SELECT count(*) FROM saloon_telemetry WHERE saloon_id=<id>;` → buffered rows landed
        `SELECT * FROM provisioning_audit_log ORDER BY changed_at DESC LIMIT 1;` → the approval
NOTICE: audit log complete = the answer to "who approved what, when".

STEP 7  Owner selects lifecycle template per saloon; engineer registers flock cycle
        (breed, start date, placement count).
EXECUTE: `curl -X POST .../saloons/1/flocks/ -d '{"breed_type":"Ross 308",
          "start_date":"<today>","placement_count":25000}'`
VERIFY: `redis-cli EXISTS saloon:1:thresholds` → 1 (threshold cache armed);
        next reading evaluates without "no thresholds" logs.
NOTICE: this single call is what arms dynamic-threshold alerting.

STEP 8  Watch 24 h WITH the tenant.
EXECUTE: after 24 h, `GET /api/v1/health/` → all services ok; acknowledge one test
         alert via `POST /alerts/{id}/acknowledge/` from THEIR worker account.
VERIFY: health 200; acknowledgement row exists; escalation paused.
NOTICE: their first question predicts your FAQ document.
```

Every step needs: exact commands/screens · expected output · rollback line. Steps that
required improvisation during the real run get written INTO the runbook before you
close the laptop — that edit is the deliverable.

### Billing placeholder

Tier stored on the org drives quota enforcement already (v0.1 seeds + cron). Record an
invoice placeholder row; Stripe integration stays Phase 2 — do not build payment
plumbing during onboarding week.

### The no-heroics rule

If any step required touching something the runbook doesn't mention: STOP after the
tenant is stable and either grow the runbook (next tenant benefits) or file the gap.
Heroics are debt with interest — GA certifies repeatability, not brilliance.

---

## 4. Cheat Sheet

| Step | Evidence | Rollback line |
|---|---|---|
| Tier row exists | SELECT returns row | n/a |
| Owner login + RLS emptiness | empty farm list | re-invite flow |
| Gateway approved | slot + creds exist | reject + recreate |
| Drain completed | rows in order | re-drain idempotent |
| Flock armed | cache populated; test eval | end_date the flock |

---

## 5. Your Subtasks

1. **Dry-run the full runbook on staging as a non-author.** Verify: succeeds without
   calling yourself. Notice: every stumble = runbook edit.
2. **Execute steps 0–3 with the real tenant.** Verify: their owner account works.
   Notice: their confidence tracks your calm.
3. **Steps 4–6 with their real hardware request.** Verify: first REAL approval through
   the extensible permission.
4. **Step 7: template selection + flock registration together with their engineer.**
   Verify: thresholds armed on their data.
5. **Sit the 24-hour watch.** Verify: their dashboard alive at hour 24 without you.
6. **Write every deviation into the runbook; note gaps to Tracker.** Verify: diff shows
   the runbook grew. Notice: this diff IS the retrospective.
7. **Tag `v1.0`. CHANGELOG tells the whole story.** Notice: read the v0.1 section of
   the same CHANGELOG before writing the last paragraph.

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| Skipping the staging dry-run | First real onboarding becomes the test |
| Hand-raising quotas verbally | Invisible ceilings / silent overages |
| Heroic fixes mid-onboarding | Next tenant hits identical wall |
| Leaving support "call my cell" | No rota = no business |
| Tagging v1.0 while gaps remain unfiled | History lies about what GA meant |

---

## 7. Prove It

1. Why must the tier row exist before STEP 1 rather than before billing? *(§3 STEP 0/
   D21.)*
2. Which v0.1 mechanism gives the new owner their very first moment of trust? *(STEP 1.)*
3. What distinguishes heroics from diligence during onboarding? Give the rule. *(§3
   no-heroics rule.)*
4. Where does the cost-per-tenant number for pricing sanity come from, and since which
   version has it existed? *(Toolbox/MET-SAAS/v0.7.)*
5. Recite the exit demo in one sentence — what exactly certifies GA? *(README exit.)*
