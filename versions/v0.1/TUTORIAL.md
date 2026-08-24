# v0.1 TUTORIAL — build the walking skeleton + schema/RLS yourself

> **How to use this file.** Read sections 1–4 to understand, then do section 5 with
> your own hands — that's where the learning is. Section 6 lists the mistakes that cost
> real hours; section 7 checks that it stuck. Companion KB lessons: *The Big Picture*
> (#/architecture), *Data Model* (#/data-model), *Security* (#/security). If this
> tutorial and `backend_docs` disagree — the docs win, and this file follows the
> **Implementation Blueprint §1–§8 pinned stack exactly**: Python 3.12,
> psycopg[binary] 3, `DATABASE_URL`/`REDIS_URL` single-env convention, 12 app packages,
> multi-stage non-root Dockerfile, coverage-gated pytest.

---

## 1. The Mission

Build the smallest honest version of Morghdari: a Django project that boots in Docker,
a CI pipeline that runs tests against a real TimescaleDB, and then the **entire ~50-table
schema with Row-Level Security** so no tenant can ever see another tenant's data.
Everything later — auth, telemetry, alerts, payments — stands on these two things.

**Done means:** CI green + `test_rls.py` proving cross-org reads return zero rows.

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| Split settings (`base/dev/test/prod`) | Four files sharing `base.py` | Environments explicit; prod fails loudly without secrets | No 5th "staging" file — staging runs production.py + env vars |
| 12 Blueprint apps under `apps/` | One app per domain (`accounts`, `tenants`, `geography`, `farms`, `devices`, `telemetry`, `alerts`, `mqtt_auth`, `provisioning`, `auditing`, `system_config`, `monitoring`) | Blueprint §1 enumeration — model homes are decided ONCE | Don't invent extra apps; don't merge them either |
| `DATABASE_URL` / `REDIS_URL` | Single-env connection convention (Blueprint §2) | Prod compose, CI compose and settings all speak the same dialect | Never reintroduce discrete `PGHOST/PGNAME` style — R3-fix killed it |
| psycopg[binary] **3** | DB driver v3 | Blueprint §2 pin; binary wheels need no compiler | Not psycopg2 — different API, superseded pin |
| TimescaleDB pg16 image | Postgres + hypertables | `saloon_telemetry` requires it from M1 | Not vanilla postgres anywhere — drift |
| docker-compose.ci.yml | Ephemeral pg+redis+pytest | THE acceptance gate | Not for daily dev |
| docker-compose.dev.yml | Persistent local services | Daily development | State persists by design — not CI |
| `set_config('app.current_org_id', %s, true)` | Transaction-scoped tenant context | RLS policies read it; `true` = dies with transaction | Never outside a transaction; never f-string the value |
| ENABLE/FORCE RLS + policy | DB-enforced row filtering | FORCE binds even the owner | App-layer checks alone are not defense |

---

## 3. The Build

### 3.1 Requirements — the Blueprint §2 pins

```
requirements/base.txt        Django>=5.0,<5.1 · djangorestframework>=3.15
                             djangorestframework-simplejwt · channels[daphne] · channels-redis
                             celery[redis]>=5.4 · django-celery-beat · paho-mqtt>=2.0,<3
                             psycopg[binary]>=3.1 · redis>=5.0 · bcrypt · prometheus-client
requirements/development.txt -r base.txt + debug-toolbar + django-extensions + ipython + ruff
requirements/test.txt        -r base.txt + pytest + pytest-django + pytest-cov + factory-boy
requirements/production.txt  -r base.txt only
```

Why: **psycopg[binary] 3** is the Blueprint driver (v2 is superseded — different API);
bcrypt must be importable because `mqtt_device_credentials` hashes are the ONLY format
EMQX verifies natively; prod image physically cannot contain debug tools thanks to the
`-r` chain.

### 3.2 Entry points

`manage.py` defaults to development locally; containers override by env.
`config/asgi.py` is the long-term entrypoint (Channels lands v0.6). Blueprint §1 also
adds `config/celery.py` (`Celery("morghdari")` + autodiscover) and an empty
`config/routing.py` (WebSocket routes arrive v0.6).

### 3.3 `config/settings/base.py` — the DATABASE_URL convention

```python
def _database_from_url(url):
    """Minimal DATABASE_URL parser — avoids an extra dependency (razor rule)."""
    parsed = urlparse(url)
    return {"ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"), "USER": parsed.username or "",
            "PASSWORD": parsed.password or "", "HOST": parsed.hostname or "localhost",
            "PORT": str(parsed.port or 5432)}

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://morghdari_dev:dev_password_only@localhost:5432/morghdari_dev")
DATABASES = {"default": _database_from_url(DATABASE_URL)}
REDIS_URL = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
```

A 10-line parser beats a dependency (razor rule). One env var carries everything;
prod compose, CI compose and docs all emit the same dialect. Then the three-DB split:

```python
CACHES["default"]["LOCATION"] = REDIS_URL + "/1"     # cache
CHANNEL_LAYERS[...]["CONFIG"]["hosts"] = [REDIS_URL + "/2"]
CELERY_BROKER_URL = REDIS_URL + "/0"                 # broker
CELERY_TIMEZONE = "Asia/Tehran"                      # AU-fix: 'midnight' crons are farm-local
```

Separate Redis logical DBs so flushing cache never eats queues. Password travels
INSIDE REDIS_URL (AU-fix — the old Channels snippet omitted it). And bcrypt joins the
hashers because `mqtt_device_credentials` hashes must be EMQX-verifiable from day one.

### 3.4 Environment overrides — Blueprint §7 exact

```python
# test.py
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]   # fast auth tests
CELERY_TASK_ALWAYS_EAGER = True                                        # tasks run inline
class DisableMigrations: ...                                           # --nomigrations partner
MIGRATION_MODULES = DisableMigrations()

if os.environ.get("DATABASE_URL"):      # PostgreSQL mode (CI sets it)
    DATABASES = {"default": _database_from_url(os.environ["DATABASE_URL"])}
else:                                   # M0 convenience: fast in-memory unit suite
    DATABASES = {"default": sqlite :memory:}
```

`DisableMigrations` + `--nomigrations`: tables come from current MODEL state, not
migration history — fast. The RLS exception (below) is why test_rls still works.

```python
# production.py — Blueprint §2 + hardening
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]       # raises if missing — fail at boot
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 2592000
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
```

### 3.5 Twelve apps, one anatomy

`apps/accounts/` … `apps/monitoring` — every package identical at M0:

```python
class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"          # MUST equal the dotted path or migrations split-brain
```

models.py/admin.py hold only comments pointing at their database.md compartment —
filled at M1.

### 3.6 Test layout — Blueprint §7

```
tests/
├── conftest.py      # fixtures: user/org/farm/saloon/gateway (M1)
├── factories.py     # factory_boy (M1)
├── unit/            # subscriber parsing/dedup/DLQ, alert engine suites (v0.3+)
├── integration/     # pipeline E2E (v0.3+)
└── test_skeleton.py # M0: settings load, 12 apps installed, admin namespace
```

pytest.ini carries the Blueprint addopts verbatim:
`--reuse-db --nomigrations --cov=apps --cov-fail-under=80 --strict-markers`.
The **RLS exception** matters from M1: policies created by migrations would never
exist under `--nomigrations`, so `test_rls.py` runs migrations-enabled (its own mark)
or applies `initdb/02_rls.sql` in a session fixture — otherwise cross-org tests pass
VACUOUSLY.

### 3.7 Dockerfile — Blueprint §8 multi-stage

```dockerfile
FROM python:3.12-slim AS builder
COPY requirements/ requirements/
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements/base.txt

FROM python:3.12-slim
RUN useradd --create-home appuser && install -d -o appuser -g appuser /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*
COPY --chown=appuser:appuser . .
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen(
    'http://127.0.0.1:8000/api/v1/health/')" || exit 1
USER appuser
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
```

Builder compiles wheels; runner installs them without a compiler. Non-root `appuser`
per §8 — and `/app` must be CREATED appuser-owned (`install -d`) with `COPY --chown`,
or pytest-cov cannot write its data file inside CI (first gate run proved it). HEALTHCHECK targets `/api/v1/health/` which exists from v0.6 — harmless
placeholder until then.

### 3.8 CI compose — DATABASE_URL dialect

Same healthcheck-gated postgres/redis; the test service speaks the new convention:

```yaml
environment:
  DATABASE_URL: postgres://morghdari_test:ci_password_only@postgres:5432/morghdari_test
  REDIS_URL: redis://redis:6379/0
command: pytest                                # coverage gate lives in pytest.ini
```

### 3.9 M1 — schema verbatim, roles, RLS everywhere

**App ↔ compartment map** (where each group of tables lives):

| Compartment | Apps | Tables |
|---|---|---|
| C1 tenancy/access | accounts, tenants | org_members, account_role_scopes (+ province_id regional scoping, 2026-08-23) (+ organizations in tenants) |
| C2 geography | geography | provinces, cities, villages |
| C2 edge/topology | farms, devices | farms, saloons, positions, photos, gateway slots/requests/hardware/zones, sensor slots, sensor+actuator hardware, actuator slots, gateway_sensor_mapping |
| C3 telemetry/time-series | telemetry | saloon_telemetry (**managed=False**, hypertable), metric catalog, hourly aggregates (**managed=False**), flock_cycles (+ placement_count/day_old_weight_g) |
| C4 alerting/config | alerts, system_config | templates, rules, alert_instances (+legal_hold), acks, **maintenance_mutes**, saloon states, tickets, mortality, prescriptions, provisioning audit, offboarding log, tier quotas, water/feed/weight/dashboard/biosecurity (D22/D23) |
| C5 commands | *(no §1 app — home decided at M1; candidate: auditing)* | command_logs (+legal_hold) |
| C6 auditing | auditing | account_activity_logs |
| C7 MQTT credentials | mqtt_auth | mqtt_device_credentials, mqtt_acl_rules |

**Transcription rule:** mirror `database.md` column-for-column; if the DDL looks wrong,
fix the DOC first (Tracker note), then the model.

**Migrate-as-owner mechanics (Blueprint §3 rule 1):** migrations run as
`morghdari_owner` so resulting tables are OWNED by a role that RLS can bind; the app
connects as `morghdari_app` (DML grants only) and NEVER as owner — PostgreSQL skips
RLS entirely for owners. Practically: `DATABASE_URL` used by `manage.py migrate`
points at the owner role (from initdb secrets), while runtime services use the app
role URL.

**Org-less global tables:** `provinces/cities/villages`, `sensor_metric_types`,
`system_configuration`, `tier_quota_mapping`, `mqtt_device_credentials`,
`mqtt_acl_rules` carry NO `org_id` — they are reference/auth data shared across
tenants, so the `tenant_isolation` policy does NOT apply (no policy created). Every
TENANT-scoped table gets ENABLE + FORCE + the standard policy. A test diffs the two
lists against database.md so nothing drifts.

**Hypertables:** `saloon_telemetry` / `metric_hourly_aggregates` are
`managed=False` models; their `create_hypertable`, `add_dimension('org_id', 4)`
and retention (180 d) DDL live in a RunSQL migration. Columnar compression is
DELIBERATELY ABSENT (C-11, owner decision 2026-08-24): TimescaleDB refuses
compression + RLS on the same hypertable, and tenant isolation wins.

Seeds stay `ON CONFLICT DO NOTHING` / get-or-create.

---

## 4. Cheat Sheet

| Command | Purpose | Key detail | Gotcha |
|---|---|---|---|
| `docker compose -f docker-compose.dev.yml up -d` | Local pg/redis/emqx | all loopback-bound | emqx dev auth = built-in-db (allow-all marked) |
| `python manage.py migrate` | Apply schema (run as OWNER url) | ~50 tables + RLS block | app-role migrate = broken ownership |
| `pytest` | Fast unit suite (sqlite unless DATABASE_URL set) | Blueprint addopts in pytest.ini | RLS tests need postgres mode |
| `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm test` | Acceptance gate | DATABASE_URL set by compose | postgres waits for HEALTHY first |
| psql as `morghdari_app` | Fail-closed inspection | BEGIN; set_config(...); ROLLBACK | no GUC ⇒ 0 rows everywhere |
| `git tag v0.1` | Version exists when tagged | after DoD sweep | tag without green CI = lie |

---

## 5. Your Subtasks

Solo, in order. Each has verification + something to notice.

1. **Create the branch + venv; install requirements.** Verify: `pip list` shows Django
   5.0.x + `psycopg` 3.x (NOT psycopg2-binary). Notice: the driver rename is Blueprint
   canon.
2. **Write the four settings files; prove production fails loudly.** Verify:
   `check --settings=config.settings.production` raises KeyError without env vars.
   Notice: boot-failure is the security design.
3. **Create the 12 app packages with correct AppConfig names.** Verify: check green +
   per-app assert test. Notice: name mismatch = duplicate-app error.
4. **Write skeleton tests; run pytest.** Verify: green WITH the 80 % coverage gate
   active. Notice: imported AppConfig classes count toward coverage.
5. **Boot dev compose.** Verify: three healthy containers. Notice: ports bound to
   127.0.0.1 — your machine only.
6. **Run the CI gate.** Verify: green inside containers. Notice: postgres was awaited
   HEALTHY before tests started.
7. **Migrate Compartments 1–2 into models (accounts/tenants/geography/farms/devices).**
   Verify: makemigrations diff matches database.md line-for-line. Notice: transcription,
   not design.
8. **Add initdb scripts (00 extensions / 01 roles / 02 rls) + the RLS migration over
   the tenant-table list.** Verify: fresh volume bootstrap clean; psql shows FORCE on
   every tenant table. Notice: org-less globals deliberately have NO policy.
9. **Write test_rls.py (four assertions) + run in postgres mode.** Verify: cross-org =
   0 rows; unset GUC = 0 rows; no pool bleed. Notice: vacuous-pass danger without the
   --nomigrations exception.
10. **Record the exit demo** (WALKTHROUGH script). Verify: CI green + fail-closed demo
    on tape. Notice: the demo makes the version real.

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| Installing psycopg2 out of habit | Import errors + wrong driver pin vs Blueprint |
| Connecting/applying migrations as the app role | Tables owned by app ⇒ FORCE RLS binds nothing meaningful; ownership chaos |
| Wrong AppConfig dotted name | Duplicate-app error, split-brain migrations |
| Forgetting the RLS --nomigrations exception | Cross-org tests pass VACUOUSLY (policies absent) |
| Treating org-less globals as tenant tables | Policy creation fails on missing org_id column |
| USE_TZ=False anywhere | Dual-timestamp audit math rots permanently |
| `from .base import *` then using an underscore name | Star-import skips `_names` — CI-only NameError on the DATABASE_URL branch |
| Trusting makemigrations deps on a VIRGIN database | Autogenerated dependencies can be incomplete; RunSQL FKs fail only on fresh volumes — replay the fresh bootstrap before closing M1 |
| Calling a @contextmanager without `with` | Silent no-op — `tenant_context(x)` sets nothing |
| ORM builders AFTER `SET ROLE morghdari_app` | WITH CHECK rejects them; build fixtures as superuser first |
| Expected-failure SQL without a savepoint | Poisons the shared test transaction — wrap in nested `atomic()` |
| CI/dev compose runs from one directory without `-p` | Same default project name replaces the dev containers (C-13) |
| Closing M1 without a db_table-vs-doc diff | Omitted tables pass silently — diff model db_table names against database.md CREATE TABLEs |

---

## 7. Prove It

1. Recite the five DB roles and the ONE sentence explaining why the app must never
   connect as the owner. *(§3.9.)*
2. Which tables intentionally have NO tenant_isolation policy, and why is that safe?
   *(§3.9 org-less globals.)*
3. `--nomigrations` is on. How does test_rls.py still see RLS policies? *(§3.6
   exception.)*
4. What parses `DATABASE_URL`, where does it live, and why no library?
   *(§3.3 razor rule.)*
5. Name both mechanisms that re-arm thresholds when a flock's end_date changes mid-day,
   and which is the backstop. *(database.md Compartment 3 transition logic.)*
