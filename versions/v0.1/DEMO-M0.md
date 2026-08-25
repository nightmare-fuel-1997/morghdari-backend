# DEMO-M0 — walking skeleton exit transcript (2026-08-24)

> EN: Captured terminal evidence for M0 per versions/v0.1/WALKTHROUGH.md.
> Text transcript = the recorded demo artifact (screencast optional, owner-side).
> فارسی: مدرک ترمینالیِ مایلستون M0 — خروجی‌ها عیناً از همان روز اجرا شده‌اند.

## 1. Backing services healthy (WALKTHROUGH §1)

```
$ docker compose -f docker-compose.dev.yml ps
SERVICE    STATUS
emqx       Up 39 minutes (healthy)
postgres   Up 12 seconds (healthy)
redis      Up 12 seconds (healthy)
```

(Note: the redis container initially showed no health status because it was a
stale container predating the M0-07 compose file; `up -d` recreated it and it
came up healthy. Also visible: the C-04-family `version` obsolete warning —
kept by decision, see CONSTRAINTS.md.)

## 2. The CI gate (WALKTHROUGH §4) — M0 EXIT GATE

```
$ docker compose -f docker-compose.ci.yml run --rm --build test
[build: builder -> test stage, wheels installed, source copied --chown appuser]
apps/accounts/apps.py                          4      0   100%
... (all 12 apps + config, full table in CI log)
TOTAL                                          60      0   100%
Required test coverage of 80% reached. Total coverage: 100.00%
============================== 3 passed in 0.53s ===============================
```

Postgres was awaited HEALTHY before pytest started (`depends_on:
condition: service_healthy`); tests ran against real TimescaleDB 2.29.2-pg16
(C-04 pin) via DATABASE_URL, not sqlite.

## 3. What the gate caught on its first runs (kept as teaching evidence)

1. `config/settings/test.py` — `from .base import *` does not import
   `_database_from_url`; the DATABASE_URL branch NameError'd only under CI
   (local sqlite runs never took that path). Fixed e089ef7.
2. Dockerfile test stage — `/app` was root-owned, so appuser's pytest-cov
   could not create `.coverage.*`. Fixed with `install -d -o appuser` +
   `COPY --chown` (e089ef7).

## 4. Not shown on purpose (scope guard, WALKTHROUGH §5)

No login/JWT, no telemetry rows, no alerts, no endpoints beyond the admin
namespace — all of that arrives v0.2+; presence here would be scope leakage.
