# v0.6 TUTORIAL — build the full REST + WebSocket surface yourself

> ⚠️ **Pre-spiral draft** — re-validate at spiral start (32 tenant endpoints is the
> frozen count; OpenAPI **v1.5.0** is the contract — the 10 §4n paths and 4 platform
> paths in the same yaml are NOT v0.6 scope). **How to use:** 1–4 teach, 5 is
> hands-on, 6 warns, 7 checks. KB lessons: *Reference Hub* (#/reference), *Use Cases*
> (#/use-cases). Canonical: OpenAPI v1.5.0 (the contract), Saas D22, Security §4.4.

---

## 1. The Mission

Implement the entire promised surface — every endpoint in the contract, live WebSocket
pushes, the KPI math, dashboard toggles, and the capability profile — then wire a test
that makes drift between code and contract IMPOSSIBLE.

**Done means:** diff test `implemented == contract` green · two browsers see the same
live updates · reconnect gap-fill merges cleanly · FCR returns NULL-with-reason before
data exists and a number after one weight sample.

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| OpenAPI diff test | CI compares router vs contract both directions | Contract violations die in CI, not in frontend integration week | Not a substitute for behavior tests |
| DRF ViewSets + routers | Bulk wiring for endpoint families | Fastest honest path; views stay dumb | Custom business logic belongs in services |
| Channels `group_send` | Post-flush push to dashboards | Decouples insert from notify | Not sub-second realtime — UI follows DB |
| `ws:active:{user}` Set + TTL | Per-user connection cap | Connection-storm defense (AU-WS-CAP) | Never a counter — Sets dedupe naturally |
| KPI service functions | D22 math as pure functions returning `(value, reason)` | Unit-testable without HTTP | Not cached at v1 — compute per request |
| Capability FEATURE_MATRIX | Declared requirements per feature | D23 made queryable at runtime | Never derive requirements inside views |

---

## 3. The Build

### 3.1 The diff test — write it FIRST, watch it fail

```python
def test_openapi_contract():
    spec = yaml.safe_load(open("openapi.yaml"))
    documented = {(p.rstrip("/") or p, m)
                  for p, ops in spec["paths"].items() for m in ops}
    implemented = {(route_path(r), m) for r in router.urls for m in http_methods(r)}
    assert implemented == documented          # BOTH directions: extras AND gaps fail
```

Red today listing all 32 missing routes; green when M6 ends. It catches missing
endpoints, extras, trailing-slash drift, method typos. When someone "just adds an
endpoint", this test demands the contract change first. **Scope note:** the gate
compares only the 32 non-platform paths — the four `/platform/support/*` endpoints
land at v0.7 (M7-07), so include them in the comparison set from that version on.

### 3.2 Endpoint families — build in this order

1. **Reads:** saloon detail/telemetry gap-fill/alerts list/hourly analytics/farm
   overview — thin views over existing services; RLS does tenancy.
2. **Operability writes:** acknowledge (+pause key transaction), operational-state
   (±2 °C ACTIVE_FLOCK guard LIVES IN THE SERVICE, not the view), flocks,
   provisioning approve/reject — delegate to v0.4/v0.5 service functions.
3. **v1.3 production logging:** weight-samples / feed-intake POST+GET;
   `UNIQUE(flock_id, entry_date)` surfaces as validation, not 500s.
4. **v1.4 additions:** water-intake POST+GET, biosecurity-events POST+GET,
   `capabilities` GET.

Family-by-family keeps the diff test partially green throughout — far better signal
than one big-bang at the end.

### 3.3 KPI service — annotated math

```python
def mortality_pct(flock):
    if not flock.placement_count:
        return None, "placement_count not set"      # D23: reason travels with NULL
    deaths = MortalityRecord.objects.filter(flock=flock)\
        .aggregate(s=Sum("count"))["s"] or 0
    return round(deaths / flock.placement_count * 100, 2), None

def fcr(flock):
    feed = FeedIntake.objects.filter(flock=flock).aggregate(s=Sum("feed_kg"))["s"]
    latest = WeightSample.objects.filter(flock=flock).order_by("-sampled_at").first()
    live = flock.placement_count - deaths(flock)
    day_old = flock.day_old_weight_g or 0           # NULL → 0 approximation (D22, ~1.6 %)
    if not (feed and latest and live):
        return None, "needs a weight sample and feed data"   # never 0, never raise
    return round(feed / ((latest.avg_weight_g - day_old) * live / 1000), 3), None
```

Every KPI returns `(value_or_None, unavailable_reason)` — that tuple IS decisions
D22+D23 expressed as types. Compliance %: hours in-band ÷ hours-WITH-data from hourly
aggregates vs flock-age rules; empty hours excluded from the denominator, not counted
bad. Water:feed ratio needs BOTH water and feed logs for the day.

### 3.4 Capability profile — declared requirements, computed truth

```python
FEATURES = {
    "fcr":         Requires(logs=["weight", "feed"]),
    "thi":         Requires(metrics={"temp", "humidity"}),
    "water_ratio": Requires(any_of=[Metric("water_l"), Log("water")], also=[Log("feed")]),
}
# GET capabilities → [{"feature": "fcr", "available": false,
#   "missing_inputs": ["weight samples"], "enable_path": "Log a sample or connect a scale"}]
```

Live metrics derive from `allowed_metric_keys` ∪ keys seen in telemetry; log
availability from row existence. No configuration table — computed truth, which is why
adding a feature later needs no migration.

### 3.5 Channels consumer — pushes with manners

```python
class TelemetryConsumer(JsonWebsocketConsumer):
    def connect(self):
        user = self.scope["user"]
        if not user.is_authenticated:
            self.close(code=4003); return                    # auth/org failure
        if redis.scard(f"ws:active:{user.id}") >= config_int("ws_max_connections_per_user"):
            self.close(code=4000); return                    # cap exceeded — don't hang
        redis.sadd(f"ws:active:{user.id}", self.connection_id)
        redis.expire(f"ws:active:{user.id}", 300)            # zombie guard, TTL-refreshed on every message
        Group(self.scope["saloon_group"]).add(self.channel_name)
```

Push side: after each batch flush Celery calls
`group_send(f"saloon_{org_id}_{saloon_id}", {"type": "telemetry.update", ...})` — the
org-scoped group name from Saas Decision 19. Dashboards update within the flush window — UI
follows DB, and DB follows the durability SLO.

Reconnect gap-fill: client sends its `last_ts`, server answers REST gap-fill, client
merges into the live stream. Server holds NO session state — statelessness makes the
merge trivially testable.

### 3.6 Dashboard-config resolution — three layers

```python
def widgets_for(user, role):
    personal = DashboardConfig.objects.filter(
        org_id=user.org_id, user_id=user.id, role=role).first()
    if personal:
        return personal.widgets
    role_default = DashboardConfig.objects.filter(
        org_id=user.org_id, user_id__isnull=True, role=role).first()
    return role_default.widgets if role_default else SYSTEM_DEFAULTS
```

Owner/admin PUT `/dashboard-config/{role}/`; users PUT only rows where the queryset
filters `user_id=request.user.id` — ownership enforced by QUERYSET, not by template
conditionals.

---

## 4. Cheat Sheet

| Piece | Purpose | Key detail | Gotcha |
|---|---|---|---|
| diff test | Contract guard | fails BOTH directions | run before writing endpoints |
| `(value, reason)` tuples | D22/D23 as types | NULL + why | never 0, never raise |
| `group_send` after flush | WS push | UI follows DB | not sub-second |
| close 4003 / 4000 | WS auth-org failure / cap | client handles both | don't hang instead |
| capabilities endpoint | D23 surface | available + missing + enable path | computed, never stored |
| config resolution | Widget toggles | personal > role > system | queryset-level ownership check |

---

## 5. Your Subtasks

1. **Copy the contract file in; write the diff test.** Verify: RED listing all missing
   routes. Notice: the failure message is your work plan.
2. **Build the READ family; watch the diff shrink.** Verify: partial-green. Notice:
   serializers copy contract examples verbatim.
3. **Build operability writes over existing services.** Verify: ack/state/flocks flows
   green. Notice: zero business logic in views.
4. **Add production-logging endpoints.** Verify: POST→GET round-trips per table.
   Notice: unique constraints become friendly validation errors.
5. **Write KPI functions + math tests BEFORE endpoints.** Verify: §2.7 suites green.
   Notice: NULL-with-reason tested explicitly.
6. **Expose kpis + dashboard-config; test 3-layer resolution.** Verify: personal >
   role > default proven. Notice: role-default writes need owner/admin.
7. **Capability service + endpoint.** Verify: unavailable→available around one sample.
   Notice: no migration needed to add features.
8. **Channels consumer + cap + gap-fill merge.** Verify: two browsers sync; overflow →
   **4000** (cap; 4003 is the auth/org code). Notice: server statelessness is what makes merges easy.
9. **Diff fully green; coverage floor met.** Verify: CI gate green. Notice: export WS
   schema notes for FE-SPEC.
10. **DoD sweep: CHANGELOG, tag `v0.6`, demo recorded.** Notice: two syncing browsers
    is the demo everyone remembers.

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| Business rules inside views | Same rule diverges across REST/WS/Celery |
| Returning 0 for missing KPIs | Decisions made on fake numbers |
| ws:active as counter | Cap under-counts; storms pass |
| Unbounded KPI ranges | Full scans behind an innocent chart |
| Skipping the diff test "temporarily" | Contract quietly stops being true |

---

## 7. Prove It

1. Why must the diff test fail in BOTH directions? Give the production incident each
   direction prevents. *(§3.1.)*
2. State the exact return contract of every KPI function with a missing input, and the
   decision that mandates it. *(§3.3/D22+D23.)*
3. Why does the capability profile need no database table? What derives it, from what?
   *(§3.4.)*
4. A user opens 11 tabs against a cap of 10. Walk connection #11 byte-for-byte, and
   state which close code a cross-org user gets and why the two differ. *(§3.5.)*
5. Where does the ACTIVE_FLOCK temperature guard live, and why is it NOT in the view?
   *(§3.2 family 2.)*
