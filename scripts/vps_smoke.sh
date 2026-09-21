#!/usr/bin/env bash
# VPS smoke test — fail-loud API proof after bring-up.
# Usage (on the VPS, from the repo root):
#   bash scripts/vps_smoke.sh
# Optional authenticated leg:
#   SMOKE_USER=<user> SMOKE_PASSWORD=<pass> bash scripts/vps_smoke.sh
#
# Checks: containers up -> nginx -> API health -> postgres -> redis ->
# contract probes -> (optional) login. Exit 0 = all green.

set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.vps.yml"
PASS=0
FAIL=0

check() { # name, command...
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "PASS  $name"; PASS=$((PASS + 1))
  else
    echo "FAIL  $name"; FAIL=$((FAIL + 1))
  fi
}

echo "== 1. containers (all Up)"
check "compose-ps" $COMPOSE ps --format json

echo "== 2. nginx on :80"
check "nginx-healthz" curl -fsS http://127.0.0.1:80/healthz

echo "== 3. API health through the stack (daphne exec probe)"
check "api-health" $COMPOSE exec -T daphne python -c \
  "import socket; s=socket.create_connection(('127.0.0.1',8000),4); s.sendall(b'GET /api/v1/health/ HTTP/1.0\r\nHost: localhost\r\n\r\n'); assert s.recv(64).startswith(b'HTTP/')"

echo "== 4. postgres ready"
check "pg-isready" $COMPOSE exec -T postgres pg_isready

echo "== 5. redis answers"
check "redis-ping" $COMPOSE exec -T redis redis-cli -a "${REDIS_PASSWORD:?set REDIS_PASSWORD in .env}" ping

echo "== 6. hardware-seam contract probes (static checks only)"
# BUG-4 (QA smoke-6, Windows): a bare `python3` resolves to the Microsoft
# Store stub on Git Bash/Windows, so this leg FAILs even when the probe is
# fine. Resolve the interpreter once, preferring python3 then python.
# فارسی: انتخابِ پویای مفسر پایتون تا در ویندوز/Git Bash هم درست کار کند.
PYTHON_BIN="$(command -v python3 || command -v python)"
check "hil-readiness" "$PYTHON_BIN" scripts/check_hil_readiness.py --skip-live

if [ -n "${SMOKE_USER:-}" ] && [ -n "${SMOKE_PASSWORD:-}" ]; then
  echo "== 7. login (authenticated leg)"
  check "auth-login" curl -fsS -X POST http://127.0.0.1:80/api/v1/auth/login/ \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$SMOKE_USER\",\"password\":\"$SMOKE_PASSWORD\"}"
else
  echo "SKIP  auth-login (set SMOKE_USER + SMOKE_PASSWORD to enable)"
fi

echo "-------------------------------"
echo "smoke: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
