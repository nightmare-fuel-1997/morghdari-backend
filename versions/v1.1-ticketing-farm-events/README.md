# v1.1 — General ticketing + farm events/reminders *(beyond v1.0, §4n 2026-09-04)*

> **Canonical plan:** `../../VERSIONS.md` §v1.1. **Not started — no code.**
> **Status pointer:** this stub exists only so `versions/` covers v0.1–v1.2;
> the build contract (TASKS/TUTORIAL/WALKTHROUGH) has not been written.

## Scope (from canonical VERSIONS.md)

- `support_tickets` generalizing `doctor_tickets` (same workflow, no external lib)
- Farm events (`farm_events` + targets + assignments) with 24h alarm+SMS
  reminders (Beat 5min dispatch + `notification_deliveries`)
- **v1.8.0-draft** growth APIs (Tracker §4p): push tokens + quiet hours,
  webhooks + deliveries, CSV imports, evidence-pack exports.
  Spec: `../../docs/api-growth-v1.md` · draft: `../../docs/releases/v1.8-growth-draft.md`
- **v1.9.0-draft** follow-through wave (Tracker §4q): F1–F36 farm/saloon/position
  CRUD, org feed + overview + search, orphan-table reads, flock close, ticket/event
  lifecycle, org-switch tokens, turnaround checklists + staff + feed + documents.
  Spec: `../../docs/api-followthrough-v1.md` · draft: `../../docs/releases/v1.9-followthrough-draft.md`
- **v1.9.0 train §4r** business numbers: KPI keys `water_ratio`/`epef`/`adg`/
  `uniformity`/`vs_standard`, `saloon_profile` + `breed_standards` +
  `vet_withdrawal_rules`, `feed_deliveries.unit_price`.
  Spec: `../../docs/business-numbers-v1.md`

## Decisions

Tracker §4n (D24-1..7, 6=A supersedes 6=B) · §4p · §4q · §4r in backend_docs
`Open Items Tracker.md`. Contract base v1.5.0; additive drafts v1.8.0/v1.9.0.

## Exit demo (canonical)

Ticket lifecycle end-to-end; event reminder delivered in-window with delivery row.
