@echo off
rem demo_live.bat — one-click LIVE demo: fake gateways/devices -> real pipeline -> dashboard.
rem No prereqs beyond Docker Desktop being INSTALLED: this script starts the dev
rem containers itself, waits for Postgres to accept connections, then migrates.
setlocal enabledelayedexpansion
cd /d "%~dp0.."

echo [1/7] dev stack (postgres + redis + emqx) ...
docker compose -f docker-compose.dev.yml up -d
if errorlevel 1 (
  echo ERROR: docker compose failed. Is Docker Desktop installed and running?
  pause
  exit /b 1
)

rem Wait for Postgres to accept connections (up to 60 s) — a freshly booted
rem container needs a few seconds; without this, migrate fails.
echo      waiting for postgres ...
set /a tries=0
:wait_pg
set /a tries+=1
docker compose -f docker-compose.dev.yml exec -T postgres pg_isready -U morghdari_dev -d morghdari_dev >nul 2>&1
if errorlevel 1 (
  if !tries! geq 30 (
    echo ERROR: postgres not ready after 60s. Check: docker compose -f docker-compose.dev.yml ps
    pause
    exit /b 1
  )
  timeout /t 2 /nobreak >nul
  goto wait_pg
)

echo [2/7] migrations ...
backend\.venv\Scripts\python.exe backend\manage.py migrate
if errorlevel 1 (
  echo ERROR: migrate failed. See output above.
  pause
  exit /b 1
)

echo [3/7] seed demo farm (idempotent) ...
backend\.venv\Scripts\python.exe backend\manage.py demo_farm_seed --manifest-out backend\farm_manifest.json

echo [4/7] start subscriber + celery worker + beat (separate windows) ...
set SUBSCRIBER_CLIENT_ID=demo-subscriber-01
set REDIS_URL=redis://:dev_redis_password@localhost:6379
start "morghdari subscriber" cmd /k "cd /d backend && set SUBSCRIBER_CLIENT_ID=demo-subscriber-01&& set MQTT_USERNAME=dev&& set MQTT_PASSWORD=devpass&& .venv\Scripts\python.exe -m morghdari.subscriber"
rem -P solo: Windows dev default (prefork/spawn) is unstable; solo is single-threaded and reliable
start "morghdari celery-worker" cmd /k "cd /d backend && .venv\Scripts\celery.exe -A config worker -l info -P solo -Q telemetry_realtime,telemetry_historical"
start "morghdari celery-beat" cmd /k "cd /d backend && .venv\Scripts\celery.exe -A config beat -l info"

echo [5/7] Django with demo UI ...
start "morghdari django" cmd /k "cd /d backend && set MORGHDARI_DEMO_UI=1&& .venv\Scripts\python.exe manage.py runserver 8000"

echo [6/7] start the feed (fake gateways + devices) ...
start "morghdari simulate-farm" cmd /k "cd /d backend && .venv\Scripts\python.exe ..\scripts\simulate_farm.py --manifest farm_manifest.json --interval 5 --mqtt-username dev --mqtt-password devpass"

echo.
echo Dashboard:  http://127.0.0.1:8000/demo/dashboard/   (demo-owner / demo-pass-123)
echo Console:    http://127.0.0.1:8000/demo/
endlocal