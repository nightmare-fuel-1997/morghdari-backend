# v0.1 WALKTHROUGH — how to run and demo it

After v0.1 you have: a booting Django skeleton, the full ~50-table RLS-enforced schema,
seeded reference data, and a green CI pipeline. There are no logins and no live data
yet — this walkthrough shows everything that DOES work.

## 0. Prerequisites

- Docker Desktop running
- Python 3.12 with a venv for local (non-docker) runs — the Blueprint §2 pin
- Copy `.env.example` → `.env` if you want to override defaults

## 1. Start the backing services

```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps        # postgres/redis/emqx = healthy
```

Observe: postgres on :5432, redis :6379 (password `dev_redis_password`), EMQX dashboard
on http://localhost:18083 (dev is `built_in_database`, no PG auth — production differs).

## 2. Run the app locally

```bash
cd backend
pip install -r requirements/development.txt
python manage.py migrate          # applies all ~50-table schema + RLS + seeds
python manage.py runserver        # http://localhost:8000
```

Opening http://localhost:8000/api/v1/ returns 404 by design — endpoints arrive in
v0.2+. The point of M0/M1 is that the server boots against the real schema.

## 3. Inspect the schema and tenancy machinery (psql)

```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U morghdari_app -d morghdari_dev
```

```sql
-- how many tables did M1 create?
SELECT count(*) FROM information_schema.tables WHERE table_schema='public';   -- ≈ 50+

-- is RLS actually forced? (should list every org-scoped table)
SELECT tablename, force_rls FROM pg_tables WHERE rowsecurity;                 -- relname varies

-- fail-closed proof: as morghdari_app with NO tenant context you see nothing
SELECT count(*) FROM saloons;                                                 -- 0 rows (no GUC set)

-- now set a tenant context the way the middleware does (transaction-scoped):
BEGIN;
SELECT set_config('app.current_org_id', '<some-org-uuid>', true);
SELECT count(*) FROM saloons;                                                -- only that org's rows
ROLLBACK;
SELECT count(*) FROM saloons;                                                -- 0 again — no leak
```

That last sequence IS the product's security foundation — watch it behave exactly so.

## 4. The CI gate (the exit demo)

```bash
docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm test      # skeleton tests + test_rls.py → green (-p isolates from dev stack, C-13)
pytest tests/test_rls.py -v                                # same suite locally against dev DB
```

Demo script (record this): show `pytest` output ending in `passed`, then repeat step 3's
fail-closed/leak-free psql sequence live.

## 5. Simulator scaffold (preview of v0.3)

```bash
python scripts/simulate_gateway.py --help    # args exist; MQTT publishing arrives in v0.3
```

## What you should NOT see

- Any login page or JWT anything (v0.2)
- Telemetry rows appearing (v0.3)
- Alerts (v0.4)

If any of those exist, scope leaked past the hardening rule.
