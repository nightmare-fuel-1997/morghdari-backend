# v0.9 TUTORIAL — run the pilot shadow yourself

> ⚠️ **Pre-spiral draft** — mostly a RUNBOOK; its quality is decided by the v0.8
> retrospective. KB lessons: all of them, live. **How to use:** 1–4 teach the daily
> mechanics, 5 is the two-week execution, 6 warns, 7 checks.

---

## 1. The Mission

Put one real gateway on one real saloon for fourteen days while the farm keeps its
existing routine — and let reality audit everything you built. You are not selling
yet; you are collecting truth.

**Done means:** 14 days · zero criticals · ≥99.9 % delivery · status page green ·
one real alert handled end-to-end by FARM staff (not you).

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| Reconciliation query (daily) | pubacked vs stored vs DLQ per day | The honesty report your owner receives each morning | Don't average across days — drift hides in averages |
| Post-mortem log line | Same-day note per page/alert | Feeds MET-ALERT-QUALITY + engine tuning | Never write "false alarm, ignore" without cause |
| Status page | Public uptime + history | Trust signal from day one | Not a marketing page — numbers only |
| Staff drill script | One alert handled by FARM staff | UX truth from real users under stress | Not during a real emergency — pick a benign window |
| Radio survey sheet | RSSI at corners before mounting | Placement beats configuration | Don't mount first and hope |

---

## 3. The Build

### 3.1 Choose and survey the farm

One saloon, cooperative owner, ≤1 h away. Survey BEFORE mounting: walk the saloon,
record RSSI at each corner, note wall materials and interference (motors, metal roofs).
Mount where the survey says, not where the ladder is.

### 3.2 The shadow contract

The farm changes NOTHING about its routine. Your system runs parallel and must earn
attention. Say this sentence to the owner out loud — it sets expectations for every
awkward moment later.

### 3.3 Daily reconciliation — the morning honesty report

```sql
-- DAILY DELIVERY % (the exit-gate metric — accountability SLO applied per day):
-- delivered = (rows stored + new DLQ entries) / messages PUBACKed, per day.
-- stored + buffered split, from the database:
SELECT count(*)                                        AS stored_rows,
       count(*) FILTER (WHERE is_buffered)             AS buffered_rows,
       round(avg(buffered_delay_mins), 2)              AS avg_delay_mins
FROM saloon_telemetry
WHERE server_received_ts >= current_date - 1;

-- numerator additions from Redis (record both numbers in the daily log):
--   dlq_delta  = XLEN telemetry:dead_letter today − yesterday
--   pubacked   = subscriber PUBACK counter delta (prometheus-client counter
--                exposed on :9091 — the AU-METRICS-SLO instrumentation)
-- delivered_pct = (stored_rows + dlq_delta) / pubacked * 100
-- ≥ 99.9 % passes; anything below gets a same-day post-mortem line.
```

Send three numbers to the owner daily: delivered %, worst gap, alerts fired. Ten words
of context. Every day, even when boring — especially when boring.

### 3.4 On-call shadowing with post-mortems

Every page/alert gets a same-day line: `date · what fired · root cause · actionable?
(y/n) · minutes-to-acknowledge`. Week 1 tells you if thresholds are right; week 2 tells
you if people trust them. False-positive trends feed MET-ALERT-QUALITY and v0.4 tuning.

### 3.5 The staff drill

Pick a benign window (not feeding time, not harvest). Let a REAL low-grade alert go to
staff only: do they receive it, understand it, act, and resolve? Write down every
friction point verbatim — that list is your UX backlog, straight from users under
mild pressure.

### 3.6 The retrospective that rewrites v1.0

Day 14 evening: reconcile the ledger (delivery, incidents, pages), review every
post-mortem line, and rewrite the v1.0 onboarding checklist from what actually
happened. Everything surprising becomes either a doc fix or a Tracker item.

---

## 4. Cheat Sheet

| Daily task | Command/query | Evidence | Gotcha |
|---|---|---|---|
| Delivery report | reconciliation SQL above | three numbers to owner | DLQ counts as DELIVERED-but-defective (DB-or-DLQ) — a rising dlq_delta forces a same-day post-mortem |
| Page post-mortem | log line same-day | actionable? y/n | never skip the y/n |
| Status page check | public URL green | history accruing | downtime must appear honestly |
| Staff drill | one alert, staff-only | their verbatim friction | not during real emergencies |
| Weekly radio re-survey | RSSI walk | compare vs day 0 | dust/season shift coverage |

---

## 5. Your Subtasks (the two weeks)

1. **Survey + mount + zero-touch on real radio.** Verify: readings flowing from the
   far corner. Notice: mounting position is a design decision.
2. **Start the daily report habit — day 1.** Verify: owner received number set #1.
   Notice: boring days build the trust exciting days will spend.
3. **Answer every page per rota; write post-mortems same-day.** Verify: log lines exist
   for each. Notice: actionable-rate is your threshold-tuning compass.
4. **Mid-run: present week-1 numbers to the owner.** Notice: which question they ask
   first is what your dashboard should have answered already.
5. **Run the staff drill in a chosen window.** Verify: end-to-end by staff, friction
   list written. Notice: do not help unless safety requires it.
6. **Keep the status page honest through any incident.** Verify: incident reflected.
   Notice: honest downtime builds more trust than fake 100 %.
7. **Day 14: reconcile the full ledger.** Verify: ≥99.9 %, zero criticals, drill done.
8. **Run the retrospective; rewrite the v1.0 checklist.** Verify: new checklist
   committed. Notice: surprises → docs or Tracker, never silence.
9. **DoD sweep: CHANGELOG, tag `v0.9`, demo = the logs themselves.**

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| Helping staff during the drill | You learn nothing about your UX |
| Skipping the report on quiet days | Trust spent exactly when you need it |
| Mounting before the radio survey | Dead corners discovered mid-flock |
| Treating false alarms as noise | Threshold tuning never happens; staff mute phones |
| Fixing found issues immediately mid-run | Changing system invalidates the 14-day dataset |

---

## 7. Prove It

1. Why must the pilot run PARALLEL to existing practice instead of replacing it?
   *(§2/§3.2.)*
2. The formula counts a reading in DLQ as DELIVERED (DB-or-DLQ accountability). So why
   does a rising dlq_delta still count as a FAILURE signal for the pilot? *(§3.3.)*
3. Name the two different things week 1 and week 2 teach you. *(§3.4.)*
4. During the staff drill, what is YOUR success metric — fast resolution or clean
   observation? Explain. *(§3.5.)*
5. A real critical fires on day 10. Does the 14-day clock reset? Defend your answer
   using the exit criteria. *(README exit demo.)*
