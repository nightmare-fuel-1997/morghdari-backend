# v0.4 TUTORIAL — build the alert engine yourself

> ⚠️ **Pre-spiral draft** — re-validate at spiral start. **How to use:** 1–4 teach, 5 is
> hands-on, 6 warns, 7 checks. KB lesson: *The Alert Engine* (#/alert-engine). Canonical:
> Saas.md §11, Walkthrough §4, Testing §2.3–2.5 + §2.4b, Blueprint §11 state matrices.
> Every bug in this version is a safety bug — the named tests ARE the spec.

---

## 1. The Mission

Turn flowing telemetry into trustworthy alarms: fire within seconds on lethal spikes,
fire at exactly 15 minutes on sustained breaches, stay silent during flapping, keep
watching when a gateway goes quiet, and never wake anyone for history. Plus the D23
rule: a feature a farm lacks simply doesn't exist — politely.

**Done means:** NH₃ velocity spike → Tier 1 <2 s · temp breach → Tier 1 at exactly
900 s · ack pauses the timer · hysteresis kills flapping · THI reports unavailable-
with-reason on sensor-less saloons · suite ≥90 %.

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| Threshold cache (`saloon:{id}:thresholds`, hash) | Flock-age-day rules in Redis | Engine reads per reading, zero DB hits | Without an ACTIVE_FLOCK flock it's absent → dynamic rules idle |
| Midnight cron + post_save re-cache | Keep cache on the right day | end_date mid-day must not kill alerting | Don't poll instead — cron + signal is the pair |
| Breach key (`saloon:{id}:breach:{metric}`) | Accumulation state w/ TTL 900 | Duration tracking across readings | TTL must be REFRESHED on consecutive breaches or alerts can never fire |
| Velocity bypass (`max_ts ≥ 50`) | Instant Tier 1 for NH₃ spikes | Lethal levels can't wait 15 min | Only for `is_tier1` metrics with max_ts |
| Hysteresis (+0.5 inside) | Resolution buffer | Kills on/off flapping | Not a second threshold — same threshold, buffered exit |
| Ack pause key (`alert:paused:{id}`) | Escalation pause w/ TTL | Workers need time to act | Not a resolution — breach may still escalate after expiry |
| LOS watchdog | Silence detection | Dead gateway ≠ healthy saloon | Suppress after Redis restarts (keys repopulating) |
| Capability gate (D23) | Rules evaluate only with live inputs | No fake data, no false alarms | Never "assume missing metric = normal" |

---

## 3. The Build

### 3.1 Threshold cache — day-aware rules in one hash

```python
def cache_saloon_thresholds(saloon_id):
    flock = FlockCycle.objects.get(saloon_id=saloon_id, end_date__isnull=True)
    day = (timezone.now() - flock.start_date).days
    template = resolve_template(flock.breed_type)   # lifecycle_templates by breed_type/is_default — flock_cycles has NO template column
    rules = LifecycleThresholdRule.objects.filter(
        template=template, day_from__lte=day, day_to__gte=day)
    mapping = {r.metric: json.dumps({"min": r.min_value and float(r.min_value),
                                     "max": r.max_value and float(r.max_value)})
               for r in rules}
    pipe.hset(f"saloon:{saloon_id}:thresholds", mapping=mapping)
    pipe.expire(f"saloon:{saloon_id}:thresholds", 90000)
```

Midnight cron re-runs it (the flock aged); a `post_save` signal on flock changes also
re-runs it immediately — setting `end_date` mid-day must not leave stale rules armed.
No active flock ⇒ key absent ⇒ dynamic evaluation early-returns. There are NO
flock-independent baselines: GS-HUMAN-SAFETY was REJECTED by the owner (2026-08-23,
Tracker §4i) — alerting is flock-gated by design, and only velocity emergencies
(Decision 10) ever cut through a suppressed state.

### 3.2 Breach accumulation — refresh or die

```python
key = f"saloon:{saloon_id}:breach:{metric}"
raw = redis.get(key)
if raw is None:
    redis.set(key, json.dumps({"started_at": now.isoformat(), "alerted": False}), ex=900)
else:
    redis.expire(key, 900)                     # THE fix: refresh every consecutive breach
    state = json.loads(raw)
if not state["alerted"] and elapsed(state) >= 900:
    fire_tier1(...); state["alerted"] = True; redis.set(key, json.dumps(state), ex=900)
```

The original design had `ex=900` set ONCE — the key expired at exactly the moment the
15-minute alert should fire, so it never could. Refresh-on-breach fixes it;
`test_breach_key_survives_exactly_15_minutes` exists so it can never regress.

Resolution uses hysteresis: clear only when the value sits **0.5 units inside** the
band (`hysteresis_buffer_units`). Flapping dies here.

Midnight transition: thresholds change while a breach accumulates — grant a 300 s
grace (`threshold_transition_grace_sec`) before judging the new band.

### 3.3 Velocity bypass — the 2-second emergency lane

```python
nh3 = payload_metrics.get("nh3", {})
if nh3.get("max_ts", -1) >= config_int("nh3_max_ts_velocity_threshold"):   # 50
    fire_alert(metric="nh3", tier=1, value=nh3["max"], source="REALTIME")
    return                                   # skips duration logic entirely
```

`max_ts` is the second-offset of the minute when the peak occurred — the sensor
screaming "lethal spike THIS minute". Config-driven (D21), NH₃-only in v1
(`is_tier1` marks candidates).

### 3.4 Escalation sweep — the heartbeat that promotes

```python
@shared_task
def escalation_sweep():
    now_ = now()
    for key in redis.scan_iter("alert:active:saloon:*"):
        st = json.loads(redis.get(key))
        if redis.exists(f"alert:paused:{st['alert_id']}"):
            continue                                  # ack pause — TTL from config
        minutes = (now_ - parse(st["fired_at"])).total_seconds() / 60
        tier = 1 if minutes < 15 else (2 if minutes < 30 else 3)
        if tier > st["current_tier"]:
            notify(st, tier)                          # worker push / engineer SMS / manager+doctor SMS
            st.update(current_tier=tier)
            redis.set(key, json.dumps(st))
```

Timeline picture:

```
t=0     breach starts ──► Tier 1 push to workers
t+15    still breaching ─► Tier 2 SMS engineer          (ack anywhere pauses clock)
t+30    STILL breaching ─► Tier 3 SMS manager + doctor
resolve past hysteresis ─► RESOLVED, timers stop
REPLAY_ANALYSIS rows ────► labeled historical, NEVER pushed
```

Ack writes `alert:paused:{id}` AND the acknowledgement row in ONE transaction
(AU-PAUSE-KEY: read-by-sweep, written-by-ack — both halves or neither).

### 3.5 LOS watchdog — silence is an alarm too

60 s sweep over expected gateways (`gateway:zones:*`): no recent traffic + 300 s grace
(`sensor:status:*`) ⇒ LOS alert to installer. After any Redis restart, suppress the
whole watchdog for `los_suppress_after_redis_restart_min` (10 min): keys repopulate
slower than the sweep runs, and a fleet-wide false page destroys trust in one morning.

### 3.6 Capability-gated rules (D23)

```python
live = set()
for slot in active_sensor_slots(saloon):          # allowed_metric_keys from saloon_sensor_slots
    live |= set(slot.allowed_metric_keys)
live |= keys_seen_in_telemetry(saloon)            # ∪ keys actually observed in JSONB metrics
# (the capability_profile SERVICE that wraps this derivation is built in v0.6 — M6-05;
#  v0.4 reads the inputs directly so the alert engine has no cross-version dependency)
if {"temp", "humidity"} <= live:
    evaluate_thi(saloon)                       # derived heat-stress index
for m in ("co", "power_mains", "power_gen"):
    if m in live:
        evaluate_rule(m)
# no humidity? THI doesn't exist for this saloon. No error. No assumption.
```

And in place of the rejected human-safety baselines (GS-HUMAN-SAFETY, owner-rejected
2026-08-23), v0.4 ships two correlation-era features: the **farm power-outage rule**
(≥ `farm_power_outage_los_ratio` of a farm's sensor slots going dark inside
`farm_power_outage_window_min` fires ONE consolidated notification and starts a
`correlation_suppress_minutes` paging-suppression window — per-sensor alerts still
log, nobody gets 40 pages for one broken fuse) and **maintenance mutes**
(`maintenance_mutes` rows silence threshold/duration alerts for planned work —
velocity emergencies always punch through).

### 3.7 Replay sweep — history without panic

After a historical batch completes: scan `is_buffered=True` rows, create
`alert_instances` with `source="REPLAY_ANALYSIS"` + `batch_id` (idempotent re-runs),
label them historical, **push nothing**. Managers review; nobody's phone buzzes for
last Tuesday.

---

## 4. Cheat Sheet

| Key / task | Purpose | Detail | Gotcha |
|---|---|---|---|
| `saloon:{id}:thresholds` | Day's rules | hash metric→{min,max} | absent = dynamic rules idle |
| `saloon:{id}:breach:{metric}` | Accumulation | TTL refreshed per breach | expire-at-900 was the original bug |
| `alert:active:saloon:{id}:{metric}` | Live alert state | current_tier, fired_at | sweep promotes, never demotes |
| `alert:paused:{alert_id}` | Ack pause | TTL = config minutes | written IN ack transaction |
| velocity check | NH₃ emergency lane | reads max_ts only | applies to is_tier1 metrics |
| replay sweep | Historical analysis | batch_id idempotent | pushes forbidden by law (D-rule) |

---

## 5. Your Subtasks

1. **Build the threshold cache + midnight cron.** Verify: cache matches today's rules.
   Notice: the signal re-cache matters MORE than the cron (mid-day changes).
2. **Write breach accumulation; then write the regression test BEFORE fixing anything.**
   Verify: `test_breach_key_survives_exactly_15_minutes` red → fix → green. Notice: you just experienced why
   the docs mandate tests by name.
3. **Add hysteresis + midnight grace.** Verify: oscillating input yields ONE alert.
   Notice: resolution is harder than firing.
4. **Implement velocity bypass; script a max_ts-57 spike.** Verify: Tier 1 <2 s in logs.
   Notice: it skipped everything — trace the code path.
5. **Build the escalation sweep + ack pause.** Verify: exact 15/30-min tier timeline in
   test fixtures (the sweep itself runs every 2 min). Notice: the pause key's TTL comes from config, never hardcoded.
6. **Add LOS watchdog; restart Redis mid-test.** Verify: no false fleet page; suppression
   window respected. Notice: DR lessons became features here.
7. **Add capability gating + THI.** Verify: sensor-less saloon logs unavailable-with-
   reason. Notice: absence handled without a single if-error.
8. **Implement the farm power-outage correlation rule + maintenance mutes** (the
   GS-HUMAN-SAFETY replacement scope). Verify: 40 simulated simultaneous LOS alerts
   produce ONE page plus a suppression window; an active mute silences threshold
   alerts but a max_ts spike still fires. Notice: rejection of a feature is also a
   decision — it's recorded, not silent.
9. **Replay sweep + idempotency test.** Verify: double-run creates nothing new. Notice:
   batch_id is the idempotency anchor.
10. **Coverage to ≥90 %; record MET-ALERT-QUALITY counters.** Verify: numbers appear in
    CHANGELOG. Notice: the retrospective will demand them.

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| TTL set once, never refreshed | 15-min alert mathematically cannot fire |
| Resolution at exact threshold | Alert/resume flapping storms |
| Ack writes row but not pause key | Escalation ignores the worker's plea |
| LOS evaluated right after Redis wipe | Fleet-wide false pages; on-call trusts you less |
| Replay sweep pushing notifications | 3 a.m. panic over last week's breach |
| Assuming missing metric = normal | Silent saloons look "healthy" |

---

## 7. Prove It

1. Two mechanisms protect the 15-minute alert from Redis expiry semantics — name both
   and their exact values. *(§3.2.)*
2. Why does the velocity bypass read `max_ts` instead of `avg`? What class of event does
   `avg` miss? *(§3.3.)*
3. A worker acks at t=16 min; the breach persists. Walk the timeline: what fires, when,
   and what did the ack buy? *(§3.4.)*
4. A saloon has temp but no humidity. State THI's behavior precisely, and which design
   decision mandates it. *(§3.6/D23.)*
5. After a catastrophic Redis wipe, list the FOUR recovery steps in order and the
   failure each prevents. *(§3.5 + DR T3.)*
