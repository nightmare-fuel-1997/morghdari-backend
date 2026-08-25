# v0.1 USAGE GUIDE — what you can actually DO with this version

> EN: A plain-language operating manual for Morghdari v0.1. Companion files:
> `WALKTHROUGH.md` (the demo script) and `RETROSPECTIVE.md` (build lessons).
> فارسی: راهنمای استفادهٔ ساده از نسخهٔ ۰٫۱ — چه چیزهایی واقعاً کار می‌کند و چطور.

---

## 1. What v0.1 IS — capabilities

Think of v0.1 as **the finished foundation of a building: everything below the
ground floor is done and inspected; there are no rooms yet.**

| Capability | Where | Since |
|---|---|---|
| Boots as containers (API image + TimescaleDB + Redis + EMQX) | Docker | M0 |
| Full database schema — all 45 documented tables | auto on `migrate` | M1 |
| Tenant isolation enforced by the DATABASE itself (RLS) | every org-scoped table | M1 |
| Five database roles (owner / app / admin / emqx / exporter) | `initdb/01_roles.sql` | M1 |
| Reference data pre-loaded: 8 metric types, 26 config keys, 3 tier quotas, geo sample | seeds migration | M1 |
| Automated test pipeline against real TimescaleDB | CI compose | M0 |
| Django admin panel (works today) | `/admin/` | M0 |
| **Interactive API documentation (Swagger UI + ReDoc)** | `/api/docs/`, `/api/redoc/` | post-v0.1 addition |
| OpenAPI schema (machine-readable contract) | `/api/schema/` | post-v0.1 addition |
| Private owner console (local HTML page) | `owner-console/serve.bat` | post-v0.1 addition |
| Gateway simulator skeleton (`--help` only) | `scripts/simulate_gateway.py` | M1 |

**What it deliberately does NOT have yet:** login/JWT (v0.2), any business API
endpoints (v0.2+), telemetry ingestion (v0.3), alerts (v0.4), payments/billing
(later). The Swagger page therefore shows a near-empty path list — that is the
honest state, and every endpoint added later will appear there automatically.

## 2. One-time setup

1. Install Docker Desktop, Python 3.12 (this machine has both).
2. Start Docker Desktop manually and wait ~10 seconds.
   ```
   start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
   docker info            # errors until the engine is ready
   ```
3. Create the Python environment (once):
   ```
   cd backend
   py -V:3.12 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements/development.txt
   ```

## 3. Daily use — three commands

```
docker compose -f docker-compose.dev.yml up -d      # 1. services up
cd backend && .venv\Scripts\python manage.py migrate  # 2. schema applied
.venv\Scripts\python manage.py runserver              # 3. app serving
```

Then open in a browser:

| URL | What you see |
|---|---|
| http://localhost:8000/admin/ | Django admin (first time: `manage.py createsuperuser`) |
| http://localhost:8000/api/docs/ | **Swagger UI** — interactive API documentation |
| http://localhost:8000/api/redoc/ | Same documentation, reference-style layout |
| http://localhost:8000/api/schema/ | Raw OpenAPI JSON/YAML for tooling |

## 4. Prove tenant isolation yourself (the security demo)

```
docker compose -f docker-compose.dev.yml exec postgres psql -U morghdari_app -d morghdari_dev
```
```sql
SELECT count(*) FROM saloons;        -- → 0  (no context = no rows: fail-closed)
BEGIN;
SELECT set_config('app.current_org_id', '00000000-0000-0000-0000-000000000000', true);
SELECT count(*) FROM saloons;        -- → only THAT farm's rows
ROLLBACK;
SELECT count(*) FROM saloons;        -- → 0 again (no leaking)
```

## 5. Run the test pipeline

```
:: fast local suite (sqlite)
cd backend && .venv\Scripts\python -m pytest

:: full gate inside clean containers (the official acceptance)
docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build test
```

⚠️ Always pass `-p morghdari_ci` on the CI command — without it, CI replaces
your running dev databases (CONSTRAINTS C-13).

## 6. Owner console (private)

Double-click `owner-console\serve.bat` → opens http://localhost:8788.
Login button is a labelled placeholder (auth arrives v0.2); the health card
stays "unavailable" until v0.6 ships `/api/v1/health/` — by design (C-06).

## 7. Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `docker info` fails | Docker Desktop not started yet — launch it, wait, retry |
| Tests hang then error in bulk | Docker died mid-run — restart Desktop, rerun |
| CI run "stole" my dev postgres | You forgot `-p morghdari_ci`; `up -d` restores dev |
| `python` picks wrong version | Use `.venv\Scripts\python` or `py -V:3.12` always |
| Health checks show backend *unhealthy* | Expected until v0.6 (C-06) — service is fine |

## 8. Where things live

- Database design: `M:\projects\poultry\backend_docs\database.md` (source of truth)
- Constraints log: `CONSTRAINTS.md` · Task history: `PROGRESS.md` · Build report: `versions/v0.1/RETROSPECTIVE.md`
