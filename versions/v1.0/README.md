# v1.0 — GA: First Paying Tenant *(beyond M8)*

> **Theme:** business — someone pays for this.
> **Risk killed:** business.
> **Duration:** ~1 week (quality decided by the v0.9 retrospective, not here)

## What this version delivers

- **Onboarding Runbook finalized** from the v0.9 retrospective and dry-run on staging
> by a non-author: org → owner → quota seed → first farm → gateway request → platform
> review → install → FM approval → flock cycle armed
- Billing placeholder live (tier from `tier_quota_mapping`; Stripe is Phase 2)
- Support/on-call rota real
- The GA sweep: CHANGELOG tells the story from empty repo to paying tenant

## Exit demo

The first tenant onboarded start-to-finish **BY THE RUNBOOK ALONE** — no heroics —
with their first flock cycle live under alerting. Any step needing improvisation
either grows the runbook or becomes a Tracker item.

## Files

[TASKS.md](TASKS.md) · [WALKTHROUGH.md](WALKTHROUGH.md) · [TUTORIAL.md](TUTORIAL.md)

## Look-back

What does v1.1 owe them first? What did onboarding cost per tenant (the honest number
MET-SAAS has been collecting since v0.7)?
