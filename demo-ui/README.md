# Morghdari v0.2 Demo Console | کنسول نمایش نسخهٔ ۰٫۲

Standalone, dependency-free web UI showcasing the **v0.2 auth & tenancy**
capabilities to a reviewer/manager. Served by Django itself so the browser and
the API share one origin (no CORS surface).

رابط وبِ مستقل و بدون وابستگی برای نمایش قابلیت‌های نسخهٔ ۰٫۲؛ توسط جنگو سرو
می‌شود تا مرورگر و API هم‌مبدأ باشند.

## Run | اجرا

```powershell
# 1. dev stack up (timescale + redis + emqx)
docker compose -f docker-compose.dev.yml up -d

# 2. migrations + a seed owner for the demo (first time only)
backend\.venv\Scripts\python.exe backend\manage.py migrate
backend\.venv\Scripts\python.exe backend\manage.py shell -c "from django.contrib.auth import get_user_model as U; u=U().objects.create(username='demo-owner'); u.set_password('demo-pass-123'); u.save(); import apps.accounts.models as m, apps.tenants.models as t; o=t.Organization.objects.first() or t.Organization.objects.create(name='Demo Farm'); m.OrgMember.objects.create(user=u, org=o, role='owner')"

# 3. start Django with the demo route enabled
set MORGHDARI_DEMO_UI=1
backend\.venv\Scripts\python.exe backend\manage.py runserver

# 4. open the console
start http://127.0.0.1:8000/demo/
```

The route is GATED: it appears only when `DEBUG` is on or
`MORGHDARI_DEMO_UI=1`. The production image never serves it.

## The guided tour | مسیر نمایش

| # | Card | Capability shown |
|---|------|------------------|
| 1 | Sign in | JWT login; decoded claims (org_id / roles / staff flag) |
| 2 | No-rotation proof | same refresh twice → both 200 (accepted risk §3.2) |
| 3 | Tamper test | flipped signature → 401 |
| 4 | Org members | list (any member) · invite/deactivate (owner-only, buttons auto-disable) |
| 5 | Offboarding chain | deactivate Session-B → B's refresh yields org-less token → reads = HTTP 200 EMPTY (C-14) |
| 6 | Support session | staff-only; blank incident 400; audited 900s scoped token |
| 7 | Event log | every request/response, timestamped |
| 8 | Telemetry explorer **(v0.3)** | RLS-scoped read API: since/until/slot_id/include_buffered filters, generated delay column |
| 9 | How your data flows **(v0.3)** | static pipeline walkthrough with every guarantee annotated |

Suggested script for the manager: log in A as `demo-owner`, log in B as a
worker you invite from card 4, walk cards 2 → 3 → 5 → 6, then run the
simulator (`python scripts/simulate_gateway.py --gateway FARM-01-GW-A --org
<org-uuid> --hardware hw-demo --packs P-A --interval 2` with MQTT_* env) and
watch card 8 fill up in real time.

## Live dashboard (fake gateways → real pipeline)

The demo branch also ships a **live dashboard** at `http://127.0.0.1:8000/demo/dashboard/`
that renders REAL charts and numbers from fake-but-realistic gateways and
sensors. Every payload travels the production path: EMQX → subscriber →
Redis lanes → Celery → TimescaleDB → RLS-scoped read API.

### One-click launcher

```powershell
backend\demo_live.bat
```

This starts the dev stack, migrates, runs the seed, opens subscriber / Celery
worker / beat / Django windows, and launches the feed simulator — then opens
the dashboard.

### By hand

```powershell
# 1. stack + migrations as above
# 2. seed the demo farm (org, saloons, slots, thresholds, demo pacing)
backend\.venv\Scripts\python.exe backend\manage.py demo_farm_seed --manifest-out backend\farm_manifest.json
#    re-runnable: --restore reverts breach pacing to the seeded 900 s

# 3. subscriber + workers (one window each)
set SUBSCRIBER_CLIENT_ID=demo-sub
backend\.venv\Scripts\python.exe -m morghdari.subscriber
backend\.venv\Scripts\celery.exe -A config worker -l info -Q telemetry_realtime,telemetry_historical
backend\.venv\Scripts\celery.exe -A config beat -l info

# 4. Django (demo routes only under DEBUG or this env)
set MORGHDARI_DEMO_UI=1
backend\.venv\Scripts\python.exe backend\manage.py runserver 8000

# 5. the feed — fake gateways + fake devices over the REAL MQTT path
backend\.venv\Scripts\python.exe scripts\simulate_farm.py --manifest backend\farm_manifest.json --interval 5
```

If your EMQX requires a dev user, export `MQTT_USERNAME` / `MQTT_PASSWORD`
before steps 3 and 5 (the feed read them from env too — see `--mqtt-username`).
The launcher assumes the dev credentials `dev` / `devpass`; create them once
via the EMQX dashboard API (login returns a token — basic auth is not the
default in EMQX 5.5+):

```powershell
$TOKEN = (curl.exe -s -X POST http://127.0.0.1:18083/api/v5/login `
  -H 'Content-Type: application/json' `
  -d '{"username":"admin","password":"dev_password"}' | ConvertFrom-Json).token
curl.exe -X POST http://127.0.0.1:18083/api/v5/authentication/password_based:built_in_database/users `
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' `
  -d '{"user_id":"dev","password":"devpass"}'
```

The seed command prints a full topology manifest; `simulate_farm.py --dry-run`
shows payloads without touching the broker. Windows note: the launcher runs
Celery with `-P solo` — the default prefork/spawn pool is unstable on Windows
dev boxes.

Scripted scenario: every few minutes a pack drifts NH₃ past the threshold
(Tier-1 after the 30 s demo pace) and an instant velocity spike fires via the
NH₃ `max_ts` lane, then everything recovers — the alert strip on the
dashboards shows it happen live. `--scenario calm` disables breaches.
