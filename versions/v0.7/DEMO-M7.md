# v0.7 DEMO-M7 — live feature drills transcript (2026-09-10)

M7-10 close evidence: every v0.7 feature exercised against the REAL rehearsal
stack (`docker-compose.prod.yml` + `rehearsal` override, laptop-as-VPS),
over real HTTPS (nginx TLS, SNI `api.morghdari.com`) and real MQTT-over-TLS
(`:8883`). Tenant tokens minted via the app's own `LoginSerializer.get_token`
over the sanctioned admin BYPASSRLS connection (the M7-13 pattern — tenant
`POST /auth/login/` 401s under the prod app role pre-GUC; VPS-day P0, unchanged).
Drill scripts live OUTSIDE the repo (`%TEMP%\opencode\v07_*.py`); secrets never
enter the repo.

## 0. Stack

All 9 prod containers healthy + monitoring (Prometheus 10/10 targets UP).
`GET /api/v1/health/` → 200 through nginx TLS. Demo Farm seeded idempotently
(`demo_farm_seed --restore`): 3 farms × 2 saloons, OPEN flocks, pacing 900/15/30.

## 1. Native lists + telemetry (M7-11/M7-12)

- `GET /api/v1/farms/` → 200, 3 FarmOverview (Baharestan/Morvarid/Nahavand, saloons embedded).
- `GET /api/v1/saloons/?farm_id=1` → 200, 2 SaloonDetail (sensor slots present).
- `GET /farms/1/telemetry/?limit=200` → rows carry `saloon_id`; farm rows (6) ==
  saloon-1 rows (6) + saloon-2 rows (0), slot sets equal → UNION-EQUALITY OK.
- `?limit=1` IS the newest (`server_received_ts` match); `?limit=N` paging is
  newest-first by design, no-param stays chronological.

## 2. Telemetry end-to-end over TLS (M7-06 recheck)

- Real schema-2.0 publish (`GW-DEMO-01`, packs P-A) over TLS 8883 → CONNACK +
  PUB-ACK → `$share/telemetry_workers/` → Redis → celery drain-kick →
  Timescale → `GET /saloons/1/telemetry/?limit=1` shows the fresh row
  (temp/nh3 live values, slot S01-PACK-A).
- Trust boundary holds: a publish whose payload `gateway_slot_id` disagreed
  with the topic gateway was PUB-ACK'd then dead-lettered
  `TOPIC_PAYLOAD_IDENTITY_MISMATCH` — spoofing dies in the pipeline, never in the DB.
- `$share` split observed live: post-rebuild publishes landed on subscriber-02
  (`telemetry_messages_received_total` 0/1 across 01/02).

## 3. Reports family (M7-15/M7-16/M7-17)

- POST DRAFT (saloon 1, flock 1) → 201, merged shape (base + extras + aliases).
- PATCH SUBMITTED with full checklist → 200; Persian literal `پایدار` normalized
  to STABLE; `reported_by`/`reported_at` stamped; bad enum → honest 400.
- POST vet-review APPROVED → 200; single `GET /{report_date}/` → 200,
  `is_completed: true`.
- POST with medication lines (name-only, no catalog) → 201 + auto-FLAGGED +
  `support_tickets` row (medical/OPEN/linked, unassigned — no doctor on Demo Farm).
- SUBMITTED with incomplete checklist → 400 `checklist incomplete`.
- W3 with old keys (`farm_slug`/`salon_slug`/`casualties_today`/`is_draft`) →
  201 normalized; W2 by-date → 200 (slugs via backfilled `legacy_slug_map`).
- Removed C-routes (`/api/compat/v1/farms/`, legacy `/api/farms/`) → 404.

## 4. Support activation (M7-07)

- Unenrolled staff login → 403 + `no confirmed MFA enrollment` pointer.
- No code → 403 `mfa_required` (`enrolled: true`); live TOTP → 200, org-less (D15);
  wrong code → 403.
- Authed setup → 200 (new secret) → confirm → 200 → login on rotated secret → 200.
- Finding: fresh-staff bootstrap is System-Admin-side only (setup/confirm need a
  token the fresh staff member cannot mint) — documented flow, not a blocker.
- Session open → 200 (org-scoped, incident, 900 s) — AFTER the tenant_context fix
  (before: 400 `unknown org_id` for every org on prod).
- Suspend `GW-DEMO-01` → 200 SUSPENDED → gateway PUBLISH denied at the broker
  (no PUB-ACK) + no DB row → resume → 200 PROVISIONED → PUB-ACK + fresh row.
- Force-resolve the live ACTIVE nh3 alert → 200 (reason mandatory — 400 without);
  second call → 200 `idempotent: true`.
- Task drain `rebuild_threshold_cache` → 202; unknown task → 400.

## 5. Observability (M7-04/M7-08) + chaos light (M7-09)

- `/api/v1/metrics/` through nginx → 404 BY DESIGN (M7-04: internal-only);
  scraped on the container network: `telemetry_dead_letter_depth 28.0` (= XLEN),
  `tenant_cost_estimate_usd{org}` exported after the rollup.
- Subscribers `:9091`: `subscriber_up 1`, `subscriber_subscribed 1` (new M7-10 gauge).
- Prometheus: 10/10 targets UP (both probers, django, subscribers, EMQX, redis…).
- Manual `build_org_usage(day=2026-09-10)`: Demo Farm msg 2 / bytes 32768 /
  saloon 1 / gateway 1 — EXACT (2 durable inserts today); idle org explicit 0.
- DLQ depth 28 < 100 → `DlqSpike` correctly inactive (full spike drilled M7-09).

## 6. Ship-blockers found + fixed (all live-proven before close)

1. `reports.0001` created `vet_medication_catalog` AFTER its FK dependent →
   real migrate died `UndefinedColumn` (commit `M7-10 live-drill fix 1`).
2. Subscriber logged "subscribed" on SUBSCRIBE-queued, never checked SUBACK — a
   boot race left both instances 74 min with ZERO broker subs while PUBs ACK'd
   into the void (commit `fix 2`: SUBACK verify + 5 s retry + gauge + tests).
3. Support-session opener's bare org lookup saw ZERO rows as the org-less app
   role (commit `fix 3`: `atomic + tenant_context` + tests).
4. Reports models lacked `managed=False` + migration state → `--check` red
   (commit `fix 4`: flags + Django-generated state folded into 0001/0002).

## 7. Honest limitations

- D24-3 next-day time-lock: live drill covers the same-day window only; the
  window-closed + staff-exempt paths are suite-proven (cannot wait for midnight).
- DLQ was NOT re-spiked to FIRING (M7-09 proved it 2026-09-08); depth/gauge/rules verified.
- Tenant login P0 stands (M7-13 finding): tokens minted the M7-13 way for drills.
- Drill artifacts in the scratch rehearsal DB only (reports 1–3, tickets, DLQ rows,
  `v07-oncall` staff — since removed; `GW-DEMO-01` drill credential re-seeded,
  password outside the repo).
