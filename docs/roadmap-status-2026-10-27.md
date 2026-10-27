# Roadmap status — where we are on VERSIONS.md, and what remains to v1.0

**Date:** 2026-10-27 · **Deployment:** vps-17 · **API contract:** 1.15.0
**Method:** every claim below was checked against the code, the live VPS, or a
run drill on 2026-10-26/27 — not against intentions.

---

## The one-paragraph answer

The original spiral plan (v0.1 → v1.0) is **done through v0.7 and half of v0.8**.
Everything that is *software* in v0.8–v0.9 has already shipped (DR drills, panels,
reports, subscriptions, aggregates — the October waves carried them early), but the
spiral ritual itself stopped being recorded after v0.5. What physically separates us
from GA is: **(1) real hardware in the loop (v0.8, in progress with the hardware
team), (2) the two-week pilot farm shadow-run (v0.9), (3) the first paying tenant
onboarded by the runbook (v1.0)**, plus four small software gaps listed at the end.

---

## Version-by-version status

### v0.1 — Walking skeleton + schema/RLS — ✅ DELIVERED (2026-08)
### v0.2 — Auth & tenancy — ✅ DELIVERED (2026-08)
### v0.3 — Ingestion + fake gateway — ✅ DELIVERED (2026-08)
### v0.4 — Alert engine — ✅ DELIVERED (2026-09)
### v0.5 — Provisioning + zero-touch — ✅ DELIVERED (2026-09-02)
   Formal record: `docs/releases/v0.5.md`; the v0.5 tag exists locally.
   *Last version with a complete spiral record (engineer→test→demo).*

### v0.6 — Full REST + WebSocket — ✅ SCOPE SHIPPED (record pending)
| Scope item | Status |
|---|---|
| All tenant endpoints (the "32 endpoints" set) | ✅ shipped — contract now carries ~5× that (1.15.0) |
| Channels WS consumer, gap-fill, ws:active | ✅ shipped (`apps/telemetry/ws*.py`, tested) |
| KPI service (mortality/compliance/FCR/cycle/water:feed) | ✅ shipped (drill-verified 2026-10-27) |
| Dashboard-config toggle resolution | ✅ shipped |
| Capability profile service (D23) | ✅ shipped (drill-verified) |
| Flutter compat adapter (v1.6.0 train) | ✅ shipped (`apps/compat/`) |
| Daily-report merge (W1–W5 aliases) | ✅ shipped |
| Ops read APIs (v1.7.0 train) | ✅ shipped (+v1.7.1/v1.7.2 increments) |
| Exit demo: OpenAPI diff test | ✅ the contract gate runs in CI and live sweeps |

### v0.7 — Real VPS + observability — ✅ SCOPE SHIPPED (live), 2 small gaps
| Scope item | Status |
|---|---|
| Production compose + nginx + emqx + redis + TLS on the real VPS | ✅ live (82.115.17.252) |
| Monitoring stack + alert rules | ✅ running (Prometheus exporter, subscriber_up) |
| Subscriber-02 replica | ✅ running |
| Support-role activation + staff MFA + 4 support-op endpoints | ✅ live-verified 2026-10-27 (MFA login + force-resolve in the drill) |
| Deploy-by-pinned-tag + one-command rollback | ✅ drilled for real (2026-10-24, deploy.sh) |
| Per-tenant cost metering (MET-SAAS) | ✅ shipped (org_daily_usage) |
| Backup scripts | ⚠️ scripts exist but **no crontab on the VPS** — backups are manual |
| External uptime prober for the 99.9 % SLO | ⚠️ status page API ships (`/api/v1/status/`) but `probers: []` — **no external prober wired** |

### v0.8 — DR + HIL real hardware — ◐ HALF DONE (the current frontier)
| Scope item | Status |
|---|---|
| wal-g in the postgres image, archive_mode on | ✅ configured; **⚠️ verify WAL archives are actually landing** (archive dir query returned 0 entries — check the wal-g push target before trusting PITR) |
| T2/T3 DR drills rehearsed | ✅ PITR + redis-wipe drill scripts exist and were rehearsed (ops/vps-day records) |
| Real ESP32+LoRa gateway + one sensor pack in the loop | ❌ **NOT STARTED — blocked on the hardware team, who are building the device now** against `docs/hardware-mqtt-protocol-v2.md`; the HIL harness (`scripts/hil_handover/`) and the firmware simulator are ready and waiting for the first real board |

### v0.9 — Pilot farm shadow-run — ◐ SOFTWARE DONE, THE PILOT ITSELF NOT STARTED
| Scope item | Status |
|---|---|
| Granular panels (panel_definitions + overrides) | ✅ shipped (migration 0007) |
| Daily reports lifecycle + vet catalog + alert subscriptions | ✅ shipped |
| Aggregates batch 1 (four daily tables + nightly writers + backfill) | ✅ shipped |
| Public status page | ◐ API shipped; needs a real prober + public host |
| **2-week shadow-run at a real farm** (≥99.9 % delivery, a real alert handled by farm staff) | ❌ **blocked on v0.8 hardware** |
| On-call shadowing + daily reconciliation reports | ❌ starts with the pilot |

### v1.0 — GA: first paying tenant — ❌ NOT STARTED (by design — gated on the pilot)
| Scope item | Status |
|---|---|
| Onboarding runbook executed start-to-finish by the runbook alone | ◐ `docs/first-install-runbook.md` exists; never executed against a real tenant |
| Billing placeholder live | ◐ usage metering shipped (`org_daily_usage`); no billing surface |
| Support/on-call rota real | ❌ |
| First paying tenant's first flock cycle with live alerting | ❌ |

### Beyond v1.0 (v1.1/v1.2 — ticketing/events, analytics)
Partial pieces shipped early (`support_tickets` generalization, aggregates batch 2
tables). Not on the critical path to v1.0.

---

## What remains until the end of v1.0 — the honest critical path

1. **Hardware in the loop (v0.8)** — hardware team delivers a real gateway + one
   sensor pack; we run the HIL harness against it; zero-touch provisioning with a
   real board survives a real power cut with FIFO replay. *In progress — the
   protocol doc (v2.0) and harnesses are ready; awaiting hardware.*
2. **Close v0.7/v0.8's three small ops gaps** — (a) install the backup crontab on
   the VPS, (b) verify WAL archiving is actually pushing (PITR is only real if the
   archive has yesterday in it), (c) wire one external uptime prober into the
   status page. *Each is hours, not days.*
3. **Pilot farm shadow-run (v0.9)** — one real saloon, real mounted gateway,
   14 consecutive clean days, ≥99.9 % delivery, one real alert handled end-to-end
   by farm staff, daily reconciliation reports, on-call shadowing. *Two weeks of
   wall-clock time once hardware exists.*
4. **v1.0 GA** — execute the onboarding runbook for the first real tenant with no
   heroics, stand up the billing placeholder, make the on-call rota real, and
   close on their first flock cycle under live alerting.

**Calendar estimate implied by the plan itself:** hardware delivery (external,
unknown) → HIL week → pilot fortnight → onboarding. The software team's own
remaining work is measured in days; the schedule is now gated by hardware and by
the pilot's two-week clock.

## Record-keeping debt (process, not product)

The spiral ritual (status update + exit demo per version) stopped being recorded
after v0.5 even though the work continued. This document is the make-up record for
v0.6–v1.0 as of 2026-10-27. Recommendation: keep the ritual lightweight but
unbroken from here — a status block per release train (the CHANGELOG already
carries one per contract version; v1.15.0 is the model to follow).
