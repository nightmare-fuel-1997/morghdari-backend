# v0.1 — Walking Skeleton + Schema/RLS *(M0 + M1)*

> **Theme:** prove the project can be built, tested and deployed — and lay the tenant-
> isolation foundation every later version stands on.
> **Risk killed:** "Can we build, test and deploy at all?" + "Is tenant isolation real
> at the data layer?"
> **Maps to:** Blueprint milestones **M0 + M1** · **Duration:** ~2–3 weeks

## What this version delivers

### M0 — walking skeleton (~week 1)
A Django project that boots, with split settings, app package placeholders, one Docker
image, a dev compose (postgres/redis/emqx) and a CI compose whose test run is green
with a trivial test. No business logic, no real tables.

### M1 — full schema + roles + RLS (~weeks 2–3)
Every table from `database.md` as migrations (**~50 tables**, including the flock-
production tables added by D22/D23), the five DB roles (`morghdari_owner`,
`morghdari_app`, `morghdari_admin`, `emqx_auth`, `morghdari_exporter`),
`ENABLE/FORCE ROW LEVEL SECURITY` with tenant-isolation policies on every org-scoped
table, initdb bootstrap scripts, seed data (metric catalog incl. the D23 keys,
system_configuration, tier quotas, provinces/cities/villages), and the gateway
simulator scaffold.

## Exit demo (the contract)

```bash
docker compose -f docker-compose.ci.yml run --rm test     # green
# plus, after M1:
pytest tests/test_rls.py -v                               # cross-org read/write → 0 rows; no GUC leak
```

## Files

| File | Read for |
|---|---|
| [TASKS.md](TASKS.md) | Every ordered task with dependencies + acceptance |
| [WALKTHROUGH.md](WALKTHROUGH.md) | How to run and demo the finished version |
| [TUTORIAL.md](TUTORIAL.md) | Line-by-line teaching guide — build it yourself |

## What exists after v0.1

A deployable skeleton with a complete, RLS-enforced schema and seeded reference data —
but no authentication, no live data path, no API surface beyond scaffolding. v0.2 puts
JWT auth and RBAC on top; v0.3 makes data flow.

## Look-back questions (retrospective)

1. Was the roles/bootstrap flow (POSTGRES_ADMIN_USER) painless anywhere it shouldn't be?
2. Did any table fight the `org_id` denormalization?
3. Pre-freeze decisions landed? (GS-CALIB was explicitly deferred by the owner.)
