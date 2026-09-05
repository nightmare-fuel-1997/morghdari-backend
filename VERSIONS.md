# VERSIONS.md — Incremental + Spiral Development Roadmap

> **Canonical version plan** (adopted 2026-08-17; **re-evaluated 2026-08-22** after the
> support-role, KPI (D22) and capability-modularity (D23) waves). **9 spirals + GA = 10
> versions** (v0.1 → v1.0), one risk class per spiral — v0.1 runs 2–3 weeks with the frozen 45-table schema (**~4–5 months to
> GA with one engineer** — the 2026-08-17 estimate of 3–4 months grew because v0.1 now
> carries 45 tables and v0.6 carries the KPI + capability services; see Revision Note).
> Mapped onto the Implementation Blueprint milestones M0–M8; v0.9 and v1.0
> extend beyond them. A spiral may not start until the previous version's demo has passed.
>
> Companion docs: `Implementation Blueprint — How to Build.md` (how to build each piece) ·
> `Open Items Tracker.md` (open risks) · `knowledge-base/` (teaching site incl. this roadmap).

---

## Revision Note — 2026-08-22 re-evaluation

What changed since adoption and where it lands:

| Change | Versions affected | Sizing impact |
| --- | --- | --- |
| Platform Support Engineer (GS-SUPPORT): role + org-context sessions, activated at v0.7 with staff MFA | v0.2 (+small), v0.7 (+medium) | ~+3 days total |
| Flock production KPIs (D22): 3 tables, `flock_cycles` columns, weight/feed logging, KPI service, dashboard toggles | v0.1 (+medium), v0.6 (+large) | ~+1 week total |
| Capability-Based Modularity (D23): water/biosecurity tables + endpoints, capability profile service, THI/gated alert rules, metric-catalog seeds | v0.1 (+small), v0.4 (+small), v0.6 (+medium) | ~+4 days total |
| Decisions closed: broilers-only v1 confirmed · GS-CALIB deferred (schema frozen without it) · GS-BOARD platform-side review (admin/support default, extensible permission) · **GS-HUMAN-SAFETY REJECTED outright** (owner, 2026-08-23 — no human-safety baselines; alerting stays flock-gated; Tracker §4i) · **Refresh-token rotation DISABLED** (owner, 2026-08-23 — SimpleJWT no rotation/no blacklist; accepted-risk note in Security §3.2) | v0.5 gains the review-permission rule; v0.4 loses the baseline-threshold work and gains correlation/mute/alert-quality specs; v0.2 loses rotation+blacklist | slightly negative (less scope) |

Net effect: **~4–5 months to GA** (was 3–4). The spiral count and order are UNCHANGED —
per the re-planning rule this is a sizing update, not a re-cut.

---

## The Spiral Ritual (every version, no exceptions)

Each version is ONE full spiral, run in this order:

1. **Objectives & risk** (½ day) — write down the one risk class this spiral kills and the
   demo that will prove it. If you cannot name the demo, the spiral is not ready.
2. **Engineer** — build the scope (below), test-first where the Blueprint names tests.
3. **Test** — the version's acceptance criteria (below) must pass; CI is the gatekeeper.
4. **Demo** — perform/record the exit demo. A version without a demo did not happen.
5. **Retrospective & re-plan** (½ day) — answer the look-back questions (below), sweep new
   findings into the Tracker, adjust the NEXT spiral's scope. This is where "spiral" earns its name: later spirals are re-planned with everything earlier spirals taught you.

**Hardening rule:** once demo prep starts, no new features enter the version — findings go
to the next spiral's plan.

**Definition of Done (per version):** acceptance tests green · demo recorded · docs +
Tracker swept · `CHANGELOG.md` entry written · git tag pushed.

---

## Version Map

| Version | Theme | Risk killed | Maps to |
| --- | --- | --- | --- |
| v0.1 | Walking skeleton + schema/RLS | Buildability; tenant isolation at the data layer | M0 + M1 |
| v0.2 | Auth & tenancy | App-layer isolation, RBAC | M2 |
| v0.3 | Ingestion + fake gateway | **The data path** (the product's core) | M3 |
| v0.4 | Alert engine | **Safety logic** (the product's reason to exist) | M4 |
| v0.5 | Provisioning + zero-touch | The trust boundary | M5 |
| v0.6 | Full REST + WebSocket | The live UX | M6 |
| v0.7 | Real VPS + observability | Operations | M7 |
| v0.8 | DR + HIL real hardware | Disaster + real radios | M8 |
| v0.9 | Pilot farm shadow-run | Reality | beyond M8 |
| v1.0 | GA — first paying tenant | Business | beyond M8 |
| v1.1 (§4n) | General ticketing + farm events/reminders | Operational follow-through | beyond v1.0 |
| v1.2 (§4n) | Lightweight analytics | Insight without a warehouse | beyond v1.1 |

---

## v0.1 — Walking skeleton + schema/RLS *(M0 + M1)*
- **Scope:** repo skeleton (split settings, app packages), Dockerfile, CI compose green;
  all 45 tables as migrations (incl. the flock-production tables: `flock_weight_samples`,
  `flock_feed_intake`, `flock_water_intake`, `dashboard_configs`, `biosecurity_events`
  + `flock_cycles.placement_count/day_old_weight_g` — MET-FCR/MET-KPI/D23, decided IN,
  manual entry); DB roles (owner/app/admin/emqx_auth/exporter),
  FORCE RLS + policies; initdb scripts; seed data (incl. the 8-key metric catalog —
  D23 adds `water_l`, `co`, `power_mains`, `power_gen`); the gateway simulator script scaffold.
- **Risk killed:** "Can we build, test and deploy at all?" and "Is tenant isolation real
  at the data layer?" — the two things everything else stands on.
- **Exit demo:** `docker compose -f docker-compose.ci.yml run --rm test` green with
  `test_rls.py`: cross-org read/write → 0 rows; GUC leak test → clean.
- **Look back:** Was the roles/bootstrap flow (POSTGRES_ADMIN_USER) painless anywhere it
  shouldn't be? Any table that fought the org_id denormalization? Pre-freeze decisions
  landed? (GS-CALIB was explicitly DEFERRED by the owner 2026-08-22 — schema frozen without it; drift risk accepted)

## v0.2 — Auth & tenancy *(M2)*
- **Scope:** SimpleJWT with org/roles claims, tenant middleware (`set_config`), RBAC
  permission classes, `/auth/login|refresh` (**no rotation, no blacklist — owner decision
  2026-08-23**, Tracker §4i), org member
  management endpoints; **Platform Support Engineer role + org-context session endpoint**
  (OpenAPI v1.2 `/platform/support/session/` — defined here, activated at v0.7; GS-SUPPORT).
- **Risk killed:** app-layer isolation and role enforcement.
- **Exit demo:** login as three roles; worker gets 403 on admin surfaces; forged/expired
  tokens rejected; a reused refresh token still yields new access tokens (no blacklist,
  accepted risk); support session opens only
  with a non-empty `incident_id` and lands a `SUPPORT_ORG_ENTER` audit row.
- **Look back:** Does the JWT org-claim model hold for installers (cross-farm scope)?
  GS-BOARD landed (platform-side review: admin/support, extensible permission)?

## v0.3 — Ingestion + fake gateway *(M3)* ⭐ core
- **Scope:** subscriber service (shared subscription, topic identity, dedup guard, DLQ,
  envelope enrichment), Redis streams + trimming, Celery consumers (realtime/historical),
  batch flush, `simulate_gateway.py` producing real schema-2.0 payloads incl. buffered
  replay storms; minimal telemetry read API.
- **Risk killed:** **the data path** — the one risk that IS the product.
- **Exit demo:** simulator → EMQX → subscriber → Redis → TimescaleDB row visible via API;
  duplicate publish → 1 row; malformed JSON → DLQ with reason; simulated 12h outage
  replays through the historical lane without delaying live messages.
- **Look back:** p99 of PUBACK→Redis under the replay storm (the fsync benchmark gate)?

## v0.4 — Alert engine *(M4)*
- **Scope:** threshold cache + midnight cron, breach accumulation (with the TTL-refresh
  regression test), velocity bypass, 15-min duration, 3-tier escalation loop,
  acknowledgements + pause key, LOS watchdog, hysteresis, replay sweep;
  **capability-gated alerting (D23)** — THI derived alerts (temp+humidity co-present),
  `co`/`power_*` rules evaluate only for saloons with live sources;
  **GS-HUMAN-SAFETY REJECTED** (owner, 2026-08-23): NO fixed human-safety baselines will
  be built — alerting remains flock/threshold-gated; velocity emergencies still bypass
  every suppression layer (Decision 10). Closed in Tracker §4i.
  **Added by the 2026-08-23 resolution session (Tracker §4i):** farm power-outage
  correlation rule (GS-CORRELATION), maintenance-mute windows (GS-MAINT-MUTE,
  `maintenance_mutes`, admin-managed), alert-quality metrics (MET-ALERT-QUALITY),
  velocity deltas read config keys (D21).
- **Risk killed:** **safety logic** — alerts that fire when they must and never when they
  must not.
- **Exit demo:** scripted NH₃ spike (max_ts 57) → Tier 1 in <2s bypassing everything;
  sustained temp breach → Tier 1 at exactly 15 min → ack pauses → Tier 2/3 timeline;
  resolution only past hysteresis; a saloon WITHOUT a humidity sensor shows THI
  "unavailable + why", never a false reading. Alert-engine unit suite ≥ 90%.
- **Look back:** Any place where Redis loss (T3) silently disabled alerting again?
  Alert quality in NUMBERS (MET-ALERT-QUALITY): MTTR, ack-to-resolve, alerts/flock
  vs. mortality.

## v0.5 — Provisioning + zero-touch *(M5)*
- **Scope:** gateway-request flow, sync endpoint (incl. `gateway_sensor_mapping`
  upserts), pending-approval buffering + drain, FM approve/reject (+ audit log + cache
  invalidation), config endpoint (Basic auth), staged credential rotation;
  **platform-side gateway-request review (GS-BOARD):** System Admin / Platform Support
  hold review by default via an EXTENSIBLE platform permission — no org-board role.
- **Risk killed:** the trust boundary — unapproved hardware can never inject data.
- **Exit demo:** unapproved gateway's telemetry sits in `telemetry:pending_approval`;
  approve → drains in order; reject → discarded; suspended gateway fails at EMQX;
  installer cannot self-approve; rotation ACK-commit dance end-to-end with the simulator.
- **Look back:** Concurrent-sync edge cases observed? State-transition gaps to codify
  (feeds AU-STATES)?
- **Status — DELIVERED 2026-09-02:** M5-01..M5-09 implemented + green on the CI gate
  (**284 passed, 3 skipped, 89.24% coverage**, TimescaleDB 2.29.2-pg16 + redis:7-alpine);
  M5-07/M5-08 additionally **live-verified against EMQX 5.5.1 (10/10)** via
  `docker compose -f docker-compose.ci.yml run --rm live-verify`.
  Release record: `docs/releases/v0.5.md` · build decisions: Tracker §4m.
  Remaining owner steps (M5-10): push the `v0.5` tag + record the WALKTHROUGH demo.

## v0.6 — Full REST + WebSocket *(M6)*
- **Scope:** all 32 tenant endpoints (v1.1 set + 5 v1.3 KPI/dashboard + 3 v1.4
  water/biosecurity/capabilities), Channels consumer (metrics-map messages, ws:active
  cap, 4000/4003 close codes),
  operational-state + flocks + acknowledge endpoints, gap-fill query; the KPI
  computation service (mortality %, compliance %, FCR, cycle summary, water:feed ratio
  — computed per request, Saas Decision 22) + dashboard-config toggle resolution +
  the **capability profile service** (`GET /saloons/{id}/capabilities/` — D23).
- **Scope addition (2026-09-05, Tracker §4o — no reorder, no new spiral):** Flutter compat
  adapter (contract **v1.6.0-draft**, additive-only): 15 compat REST routes + farm WS shim
  under `/api/compat/v1` (legacy `/api/...` aliases), `legacy_slug_map` as the single
  schema addition, metrics-map → flat-record translation. Spec: `docs/api-compat-flutter-v1.md` ·
  release draft: `docs/releases/v1.6-compat-draft.md` · history: `CHANGELOG.md [Unreleased → 1.6.0]`.
- **Scope addition (2026-09-05, Tracker §4p — no reorder, no new spiral):** ops read APIs
  (contract **v1.7.0-draft**, additive-only): pagination convention, audit-log query,
  threshold read/update, saloon photos, gateway reads + diagnostics, password change +
  sessions (`user_sessions` — the only new table). Spec: `docs/api-ops-v1.md` ·
  release draft: `docs/releases/v1.7-ops-draft.md` · history: `CHANGELOG.md [Planned → 1.7.0]`.
- **Risk killed:** the live UX contract.
- **Exit demo:** OpenAPI diff test `implemented == contract`; a browser tab receives
  `telemetry.update` every 60s from the simulator; reconnect gap-fill merges cleanly;
  the capabilities endpoint reports FCR unavailable-with-reason on a saloon with no
  weight samples, and available after one is logged.
- **Look back:** WS message schema stable enough to freeze for the Frontend Integration
  Spec (open item)?

## v0.7 — Real VPS + observability *(M7)*
- **Scope:** production compose + nginx + emqx + redis configs live, TLS issuance,
  monitoring stack deployed, alert rules wired, backup scripts + cron, subscriber-02;
  **support-role activation + MFA for platform-staff accounts + the 4 v1.2 support-op
  endpoints** (GS-SUPPORT); **external uptime prober for the 99.9 % API SLO +
  per-tenant cost metering** (MET-AVAIL/MET-SAAS).
- **Risk killed:** operations — the system runs and screams when it's sick.
- **Exit demo:** deploy from CI by pinned tag; health 200; kill the subscriber → the
  `subscriber_up < 2` page fires; DLQ spike drill; rollback to the previous tag in one
  command; support engineer (MFA) enters an org via audited session and suspends a
  gateway — superuser untouched.
- **Look back:** Disk growth vs the capacity model (AU-DISK-MATH) — real numbers now?

## v0.8 — DR + HIL real hardware *(M8)*
- **Scope:** wal-g baked into the postgres image (archive_mode on), T2/T3 drills
  rehearsed on staging, real ESP32+LoRa gateway + one sensor pack in the loop.
- **Risk killed:** disaster recovery and the gap between simulated and real radios.
- **Exit demo:** PITR restore drill logged; Redis-wipe drill → alerting back within the
  window; real gateway provisions zero-touch, streams real readings, survives a real
  power cut (FIFO replay with original timestamps).
- **Look back:** HIL findings → firmware spec corrections needed?

## v0.9 — Pilot farm shadow-run *(beyond M8)*
- **Scope:** one real saloon, real gateway mounted, 2 weeks shadow-run (parallel with
  any existing practice), daily reconciliation reports, on-call shadowing;
  **public status page live** (status.morghdari.com — MET-STATUS).
- **Scope (§4n addition, 2026-09-04):** granular panels (`panel_definitions` + role/user
  overrides; reviewer read-only; default-open; owner admin panel) + daily reports
  (`daily_reports` lifecycle + Tehran-day time-lock) + per-org vet catalog
  (`vet_medication_catalog`, vet-only writes) + alert subscriptions
   (`alert_subscriptions` — closes GS-ROUTING at pilot urgency).
- **Scope addition (2026-09-05, Tracker §4p):** aggregates batch 1 (four daily tables +
  nightly writers + exact backfill — `metric_daily_aggregates`, `flock_daily_snapshots`,
  `alert_daily_stats`, `gateway_daily_uptime`). Spec: `docs/data-aggregates-v1.md`.
- **Risk killed:** reality — radio coverage, dust, power, humans.
- **Exit demo:** 14 consecutive days with zero critical incidents, ≥ 99.9% delivery
  (pubacked readings in DB-or-DLQ — the pilot ACCEPTANCE gate; the standing SLO is
  ≥ 99.99%, Observability §2), and at least one REAL alert handled end-to-end by
  the farm staff.
- **Look back:** Everything. This retrospective rewrites the v1.0 checklist.

## v1.0 — GA: first paying tenant *(beyond M8)*
- **Scope:** onboarding runbook executed for a real org (org + owner + quota seed +
  first farm), billing placeholder live, support/on-call rota real.
- **Risk killed:** business — someone pays for this.
- **Exit demo:** the first tenant onboarded start-to-finish BY THE RUNBOOK ALONE (no
  heroics), their first flock cycle with live alerting.
- **Look back:** What does v1.1 owe them first?

## v1.1 — General ticketing + farm events/reminders *(beyond v1.0, §4n 2026-09-04)*
- **Scope:** `support_tickets` generalizing `doctor_tickets` (same workflow, no external
  lib) + farm events (`farm_events` + targets + assignments) with 24 h    alarm+SMS
  reminders (Beat 5 min dispatch + `notification_deliveries`).
- **Scope addition (2026-09-05, Tracker §4p):** growth APIs (contract **v1.8.0-draft**,
  additive-only): push tokens + quiet hours, webhooks + deliveries, CSV imports,
  evidence-pack exports (closes GS-EVIDENCE). Spec: `docs/api-growth-v1.md` ·
  release draft: `docs/releases/v1.8-growth-draft.md` · history: `CHANGELOG.md [Planned → 1.8.0]`.
- **Scope addition II (2026-09-05, Tracker §4q):** follow-through wave (contract
  **v1.9.0-draft**, additive-only): farm/saloon/position CRUD, org feed + overview + search,
  orphan-table reads, flock close, ticket/event/catalog lifecycle, org-switch tokens,
  turnaround checklists + staff assignments + feed deliveries + documents. Spec:
  `docs/api-followthrough-v1.md` · release draft: `docs/releases/v1.9-followthrough-draft.md` ·
  history: `CHANGELOG.md [Planned → 1.9.0]`.
- **Scope addition III (2026-09-05, Tracker §4r — same v1.9.0 train):** business numbers —
  KPI keys `water_ratio`/`epef`/`adg`/`uniformity`/`vs_standard`, tables `saloon_profile` +
  `breed_standards` + `vet_withdrawal_rules`, `feed_deliveries.unit_price` fix. Spec:
  `docs/business-numbers-v1.md`.
- **Risk killed:** operational follow-through — tickets and reminders survive restarts.
- **Exit demo:** ticket lifecycle end-to-end; event reminder delivered in-window with a delivery row.
- **Look back:** Are ticket categories sufficient, or does v1.2 analytics need new ones?

## v1.2 — Lightweight analytics *(beyond v1.1, §4n 2026-09-04)*
- **Scope:** lightweight analytics via `analytic_insights` computed on existing aggregates
  (no warehouse, no new pipeline).
- **Scope addition (2026-09-05, Tracker §4p):** aggregates batch 2 guarantees the inputs —
  `org_daily_usage` + `notification_daily_stats` (built on the v1.1 train). Spec:
  `docs/data-aggregates-v1.md`.
- **Risk killed:** insight without infrastructure — numbers from data already stored.
- **Exit demo:** at least one insight rendered from aggregates with its computation traceable.
- **Look back:** Which insights earned a permanent dashboard slot?

---

## Versioning policy
- Git tags: `v0.1` … `v1.0` — a version exists when its tag is pushed.
- One `CHANGELOG.md` entry per version (demo link included).
- The API `version` field in `/api/v1/health/` echoes the current tag.
- Docs and the Knowledge Base roadmap page are updated in the same commit as the tag
  (the KB shows this file's mapping).

## Re-planning rule
At every retrospective the NEXT spiral's scope may be re-cut (that is the spiral model),
but the order of risk classes does not change without a written reason in the Tracker —
the order IS the architecture's dependency chain.
