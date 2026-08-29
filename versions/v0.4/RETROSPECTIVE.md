# v0.4 RETROSPECTIVE — Alert engine (handoff to owner)

> EN: Full inventory of every place the M4 build diverged from the corpus or
> forced an interim decision, what was done during the build, and the suggested
> permanent resolution. Same ritual as v0.1: the owner concludes → docs are
> revised → then (and only then) v0.5 implementation starts.
>
> فارسی: فهرست کاملِ مواردی که ساختِ نسخهٔ ۰٫۴ تصمیمِ موقت گرفت — مالک تصمیم
> می‌گیرد، بعد داکیومنت‌ها بازنویسی می‌شوند، بعد نسخهٔ بعدی شروع می‌شود.

> **STATUS (2026-08-29): OPEN — awaiting owner decisions on R1–R5.**
> Q1–Q10 were already approved doc-first during planning (backend_docs Tracker
> §4l, all approved 2026-08-26); nothing from §4l re-opens here.

Status legend:
- **[SETTLED]** — resolved during the build; recorded for awareness only.
- **[OWNER DECISION]** — interim fix shipped; permanent choice is yours.
- **[DOC REVISION PENDING]** — mechanical doc edit queued behind your conclusion.

---

## Part 1 — Open items from the M4 build

### R1. Maintenance-mute digest has no home in `account_activity_logs` — actor FK is NOT NULL [OWNER DECISION]

**Where it bit:** M4-10 (GS-MAINT-MUTE), `apps/alerts/services/mutes.py`.

- Corpus (database.md) places the end-of-window digest line in
  `account_activity_logs`.
- Reality: that table's `actor` FK is NOT NULL, and the mute digest is a
  SYSTEM-authored event — there is no human user to reference. Writing the row
  would require either a nullable column, a system pseudo-user, or a different
  table.
- Interim fix shipped: the digest is a structured WARNING log line emitted by
  the escalation sweep (NX-guarded, exactly once per ended window), with the
  suppressed-count held in Redis per Tracker §4i ("storage via config").
  Zero schema change.
- Impact if kept: digests are not queryable via SQL and don't appear in the
  activity-log surface; they survive only in log aggregation.
- Options:
  - **(a)** Keep the log-line digest permanently. Cheapest; the count also
    lives in Redis until read. Accept that it's outside the DB.
  - **(b)** Make `account_activity_logs.actor` nullable + add a CHECK-ish
    convention (system rows have NULL actor and a distinct `action` prefix).
    Honest modeling; touches a high-traffic audit table.
  - **(c)** Create a dedicated system pseudo-user (e.g. `system@morghdari`)
    and keep the FK NOT NULL. No schema change to the audit table, but a fake
    user row pollutes the accounts surface and RLS/tenancy must be checked
    for it.
  - **(d)** New dedicated table (e.g. `system_event_log`) for machine-authored
    events. Cleanest separation, but a new table + RLS policy for one digest
    line is heavy.
- Recommendation: **(b)** — system-authored events are a permanent class (this
  won't be the last one; LOS recovery and replay dispatch are also
  machine-authored), and a nullable actor with a documented convention is the
  smallest honest model. Doc-first change to database.md + migration + RLS
  parity re-check.
- DOC REVISION PENDING: database.md account_activity_logs section (actor
  nullability + system-row convention) once ruled.

### R2. NRC THI constant not pinned by the corpus [OWNER DECISION]

**Where it bit:** M4-09 (D23 capability-gated rules), `apps/alerts/services/capability.py`.

- The corpus (Saas.md D23 activation matrix) pins the THI RULE (derived from
  co-present temp + humidity) but NOT the formula constant.
- Interim fix shipped: the industry-standard NRC form was implemented:
  `THI = (1.8·T + 32) − (0.55 − 0.0055·RH)·(1.8·T − 26)` (T in °C, RH in %).
  Recorded in CHANGELOG [v0.4] for ratification.
- Impact if unratified: the threshold `thi_max` in lifecycle rules is
  calibrated against THIS formula; swapping formulas later would silently
  change when heat-stress alerts fire.
- Options:
  - **(a)** Ratify the NRC form as-is (recommended — it is the poultry-industry
    standard and matches the corpus's intent).
  - **(b)** A different formula (e.g. the simpler `THI = 0.8·T + RH·(T−14)/4`
    variant used in some livestock literature) — would need re-calibration of
    `thi_max` seeds.
- Recommendation: **(a)**. Doc-first change: pin the formula in database.md /
  Saas.md D23 so the constant is corpus-owned from now on.
- DOC REVISION PENDING: Saas.md D23 + database.md metric catalog annotation.

### R3. `test_capability_profile` deferred to M6 (Tracker §4l-Q5) [SETTLED — awareness]

- The canonical capability_profile service (a per-saloon inventory of which
  metrics are live) was deliberately scoped out of v0.4; engine-local gating
  (`metric_is_live`) shipped instead.
- Nothing to decide — this is a recorded deferral to M6, already approved in
  Tracker §4l-Q5. Listed here so the retrospective is complete.
- No doc revision pending (Tracker §4l already records it).

### R4. Realtime drain kick 10 s → 2 s (Tracker §4l-Q7) [SETTLED — awareness]

- Settings-only change (`CELERY_BEAT_SCHEDULE`), approved doc-first per the
  §4k-Q5 precedent, so the Tier-1 "<2 s" exit-demo claim holds end to end.
- Historical lane deliberately keeps its 10 s kick.
- No decision needed; noted for completeness. If historical pacing ever needs
  the same treatment, it's a one-line settings bump.

### R5. Django 5.0 removed `ExtractEpoch` — Python-side duration averaging [SETTLED — awareness]

- MET-ALERT-QUALITY (M4-11) originally assumed SQL-side epoch extraction;
  Django 5.0 removed `ExtractEpoch`.
- Fix shipped: fetch timestamp pairs and average durations in Python — exact
  on the Postgres prod path AND the SQLite CI fallback (no native duration
  extraction on sqlite). Coverage 98% on `quality.py`, 9/9 tests green.
- Long-term option (only if metric volume ever makes Python averaging slow):
  `Extract` with custom lookups or raw SQL on the Postgres path with a
  SQLite branch. Not worth it at current scale.
- No doc revision pending; CHANGELOG [v0.4] already records the constraint.

---

## Part 2 — Proposed doc/guide revisions (apply ONLY after your conclusions)

For backend_docs + version folders, once R1 and R2 are ruled:

1. `database.md` account_activity_logs: apply the R1 decision (nullable actor
   + system-row convention, or the chosen alternative) and update the
   GS-MAINT-MUTE digest target.
2. `Saas.md` D23 + `database.md` metric catalog: pin the THI formula constant
   (R2) so the corpus owns it.
3. `apps/alerts/services/mutes.py`: if R1 = (b), swap the WARNING log line for
   the real `account_activity_logs` row (system-authored, NULL actor) and
   retire the Redis-held count after write.
4. CHANGELOG [Unreleased]: record both resolutions like the v0.1 post-retro
   sweep did (C-08/C-10/C-11 pattern).

---

## Part 3 — Decision card (answer these two)

| # | Question | Recommendation |
|---|---|---|
| R1 | Maintenance-mute digest home: system-authored rows in `account_activity_logs` | (b) nullable actor FK + documented system-row convention |
| R2 | Pin the NRC THI formula as the corpus constant | (a) ratify `THI=(1.8T+32)−(0.55−0.0055·RH)(1.8T−26)` |

R3–R5 need no decision (deferral + two settled constraints, already recorded).

Reply "agree with both" (or amend individually) → docs get revised → then
v0.5 planning starts on your go.
