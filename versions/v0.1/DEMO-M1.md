# DEMO-M1 — schema + RLS exit transcript (2026-08-24)

> EN: Terminal evidence for the M1 exit demo per versions/v0.1/WALKTHROUGH.md §2–§5.
> فارسی: مدرک دموی خروج M1 — شمارش جدول‌ها، وضعیت RLS و توالیِ بسته-به-خطا.

## 1. Schema size (WALKTHROUGH §3)

```
$ psql -U morghdari_dev -d morghdari_dev
SELECT 'tables: '||count(*) FROM information_schema.tables WHERE table_schema='public';
 tables: 61
```
(45 doc tables + accounts_user + Django contrib/celery-beat machinery.)

## 2. RLS coverage

```
SELECT count(*) FROM pg_class c ... WHERE relrowsecurity AND relforcerowsecurity;
 rls_forced: 35          -- every org-scoped table, ENABLE + FORCE
```
Parity guard: `python scripts/check_rls_parity.py` → 35/35, 1:1 across
database.md ↔ initdb/02_rls.sql ↔ tenants/0002.

## 3. Fail-closed proof, live (the product's security foundation)

```
$ psql -U morghdari_app -d morghdari_dev        -- socket login, no password
SELECT 'no_guc_saloons: '||count(*) FROM saloons;
 no_guc_saloons: 0                              -- fail-closed by default

BEGIN;
SELECT set_config('app.current_org_id','11111111-1111-1111-1111-111111111111',true);
SELECT 'with_guc: '||count(*) FROM saloons;
 with_guc: 0                                    -- that org simply owns no rows yet
ROLLBACK;
SELECT 'after_rollback: '||count(*) FROM saloons;
 after_rollback: 0                              -- no leak across transactions
```
The scripted equivalents (cross-org SELECT=0 / INSERT blocked / no pool bleed)
run in `backend/tests/test_rls.py` — green inside CI against fresh TimescaleDB:
14 passed @ 84.6% coverage (`docker compose -f docker-compose.ci.yml run --rm test`).

## 4. Simulator scaffold (WALKTHROUGH §5)

```
$ python scripts/simulate_gateway.py --help      # works dependency-free
$ python scripts/simulate_gateway.py --gateway FARM-01-GW-A
[scaffold] would simulate gateway 'FARM-01-GW-A' against localhost:1883 every 60s
[scaffold] NOT publishing — real simulation is a v0.3 deliverable
```

## 5. Operational notes captured during the round

- C-13: running the CI compose from this directory REPLACED dev's postgres/redis
  containers (shared default project name). Always pass `-p morghdari_ci`;
  dev restores via plain `up -d` from its named volume.
- Fresh-volume bootstrap replayed end-to-end twice during the round
  (down -v → initdb → 58 migrations → 35/35 FORCE'd).
