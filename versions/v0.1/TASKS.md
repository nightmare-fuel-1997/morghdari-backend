# v0.1 TASKS — ordered breakdown (M0 → M1)

Legend: each task has an ID, dependencies, and an acceptance check. A task is done when
its acceptance passes, not when the code "looks right".

## M0 — walking skeleton

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M0-01 | Git branch `v0.1-m0` created; `backend/` folder exists | — | `git branch --show-current` = v0.1-m0 |
| M0-02 | Requirements per Blueprint §2 pins: Django 5.0, DRF, simplejwt, channels[daphne]+channels-redis, celery[redis], django-celery-beat, paho-mqtt 2.x, **psycopg[binary] 3**, redis, bcrypt, prometheus-client; dev/test extras per §2 | M0-01 | fresh venv install succeeds; `pip list` shows psycopg 3.x (not psycopg2) |
| M0-03 | Project scaffold: `manage.py`, `config/__init__.py`, `config/urls.py`, `config/asgi.py`, `config/wsgi.py` | M0-02 | `python manage.py check` passes |
| M0-04 | Settings split with **DATABASE_URL / REDIS_URL single-env convention** (Blueprint §2); TIME_ZONE + CELERY_TIMEZONE `Asia/Tehran`; test.py = MD5 hasher + eager Celery + DisableMigrations + DATABASE_URL-conditional postgres | M0-03 | all four settings modules pass check; production raises without env vars |
| M0-05 | The **12 Blueprint §1 app packages** (`accounts, tenants, geography, farms, devices, telemetry, alerts, mqtt_auth, provisioning, auditing, system_config, monitoring`) with AppConfig `name="apps.X"` + models/admin/migrations placeholders | M0-04 | check green; per-app INSTALLED_APPS assert green |
| M0-06 | Trivial test: `tests/test_skeleton.py` (settings load; admin URL namespace exists) + `pytest.ini` (`DJANGO_SETTINGS_MODULE=config.settings.test`) | M0-05 | `pytest -q` green locally against sqlite fallback |
| M0-07 | `docker-compose.dev.yml` copied verbatim from docs repo (timescale pg16 :5432, redis :6379 everysec+password, emqx 1883/8883/18083 built-in-db) | M0-01 | `docker compose -f docker-compose.dev.yml up -d` → all healthy |
| M0-08 | **Multi-stage Dockerfile per Blueprint §8**: python:3.12-slim builder (wheels) → runner as non-root `appuser`, HEALTHCHECK `/api/v1/health/`, single image four commands + `.dockerignore` | M0-02 | `docker build ./backend` succeeds |
| M0-09 | `docker-compose.ci.yml` adapted from docs repo: builds `./backend`, timescale pg16 + redis with healthchecks, runs pytest with coverage ≥ trivial threshold | M0-06, M0-08 | **`docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm test` GREEN** ← M0 exit gate |
| M0-10 | `.env.example` documenting every variable the settings read; `initdb/` placeholder dir | M0-04 | Every env var in settings appears in `.env.example` |
| M0-11 | Commit on `v0.1-m0`: `M0: walking skeleton, CI green` | all M0 | push/branch tidy; demo recorded per WALKTHROUGH |

## M1 — full schema + roles + RLS

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M1-01 | Models for Compartments 1–2 verbatim from `database.md` (orgs, members, geo, farms, saloons, role scopes, gateway slots/requests/hardware/zones/positions/sensor-slots/hardware/mapping/photos, actuators ×2) | M0-05 | `makemigrations` generates clean migrations; models match DDL column-for-column |
| M1-02 | Compartment 3: telemetry hypertable model + metric catalog + hourly aggregates + flock_cycles (+ `placement_count`, `day_old_weight_g`) — TimescaleDB extension enabled via initdb migration | M1-01 | `create_hypertable` statements run inside a migration RunSQL against the CI timescale image; retention 180 d active; NO compression (C-11 decided: RLS wins) |
| M1-03 | Compartments 4–7: thresholds/templates, alert_instances, system_configuration (+ full seed INSERT list), acks, saloon states, doctor tickets, mortality, prescriptions, provisioning audit, offboarding log, tier quotas, command_logs, activity logs, mqtt credentials/acl | M1-01 | Row count of migrations == table count in database.md (~50); nothing invented beyond docs |
| M1-04 | D22/D23 tables: `flock_weight_samples`, `flock_feed_intake`, `flock_water_intake`, `dashboard_configs`, `biosecurity_events` | M1-03 | Present in migrations; org_id + RLS-ready |
| M1-05 | initdb scripts per Blueprint §1 layout: `00_extensions.sql` (timescaledb), `01_roles.sql` (five roles; app role DML-only), `02_rls.sql` (ENABLE/FORCE/POLICY over tenant-table list) | M0-09 | fresh CI postgres volume bootstraps cleanly; roles exist with correct privileges |
| M1-06 | RLS migration: for EVERY tenant-scoped table — `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, policy `tenant_isolation USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID) WITH CHECK (...)` | M1-01..04, M1-05 | A generated policy list matches 1:1 the org-scoped tables in database.md (script-checked) |
| M1-07 | Seed data: sensor_metric_types (8 keys), system_configuration (full canonical key list), tier_quota_mapping, provinces/cities/villages sample | M1-03, M1-05 | Seeds load idempotently (`ON CONFLICT DO NOTHING`); health of seeds checked by a test |
| M1-08 | DB connection as `morghdari_app` (non-owner!) everywhere the app connects; GUC helper `tenant_context(org_id)` contextmanager | M1-05 | App role CANNOT see rows without GUC (fail-closed test) |
| M1-09 | `tests/test_rls.py`: two-org fixture; assert cross-org SELECT=0, cross-org INSERT blocked, GUC unset ⇒ 0 rows, GUC leak between pooled connections impossible (transaction-scoped `set_config(..., true)`) | M1-06, M1-08 | **`pytest tests/test_rls.py` GREEN in CI** ← M1 exit gate |
| M1-10 | Gateway simulator scaffold: `scripts/simulate_gateway.py` argparse skeleton (gateway id, interval; publishes placeholder payload to local EMQX topic) | M0-07 | `--help` works; actual MQTT publish lands in v0.3 |
| M1-11 | Version DoD sweep: CHANGELOG.md entry, tag `v0.1`, docs/KB swept, demo recorded | M1-09, M1-10 | Tag exists; CHANGELOG lists risk killed + demo |

## Standing constraints during v0.1

- Models mirror `database.md` **verbatim** — no column renames, no "improvements".
  If the DDL looks wrong, change the DOC first (with a Tracker note), then the model.
- The Django app connects as `morghdari_app`, never the owner/admin.
- No auth, no endpoints, no business logic — resist scope creep; park ideas in the
  Tracker (hardening rule).
