#!/usr/bin/env bash
# start_demo_stack.sh — one-command LIVE-demo stack launcher (Windows Git Bash).
#
# Brings up, in order:
#   1. EMQX broker (Docker, MQTT :1883 + dashboard :18083) — reused if already running
#   2. Demo farm seed (idempotent) + fresh manifest
#   2.5 EMQX authn + topic ACLs (scripts/emqx_provision.py — per-farm sim users,
#       subscribe-only subscriber, no_match=deny)
#   3. Django backend on :8000 (the demo-ui React app is served from /demo/)
#   4. scripts/simulate_farm.py — fake devices publishing over the REAL MQTT path
#   5. celery worker + beat — drains the MQTT streams into Postgres
#   6. MQTT subscriber (demo-subscriber-01, demo-sub/devpass) — stream ingress
#
# Usage:
#   bash scripts/start_demo_stack.sh            # start everything
#   bash scripts/start_demo_stack.sh --calm     # simulator without breach scenarios
#   bash scripts/start_demo_stack.sh --stop     # stop all above (EMQX kept)
#
# Logs: backend/demo-server.log, backend/demo-sim.log,
#       backend/celery-worker.log, backend/celery-beat.log, backend/demo-subscriber.log
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
PY="$BACKEND/.venv/Scripts/python.exe"
DB_URL="postgres://morghdari_dev:dev_password_only@localhost:5432/morghdari_dev"
MANIFEST="$BACKEND/farm_manifest.json"
EMQX_CONTAINER="poultry_the_main_by_ai-emqx-1"

log() { printf '[demo-stack] %s\n' "$*"; }

# ── stop path ────────────────────────────────────────────────────────────────
# NOTE on pid files: every launch block writes `echo $! > "$BACKEND/...pid"`
# with the ABSOLUTE path — a bare filename would land in the CALLER's cwd
# (the `&` backgrounds the `cd && ...` list, so the echo runs outside $BACKEND)
# and the guards/--stop below would never see it. That is how a second
# invocation could silently duplicate the whole stack.
if [ "${1:-}" = "--stop" ]; then
  # Match components by COMMAND LINE, not pid files: the pid files store MSYS
  # pids and venv python.exe is a redirector that spawns a separate real
  # interpreter, so a pid-based kill keeps leaving half the tree alive. wmic
  # finds BOTH processes of a component (launcher + real python) and taskkill
  # //T takes each down — immune to stale/missing pid files as well.
  stop_component() {
    local sig="$1" label="$2"
    local pids
    pids=$(wmic process where "name='python.exe' and CommandLine like '%${sig}%'" get ProcessId 2>/dev/null \
      | tr -d '\r' | grep -oE '[0-9]+' | sort -u) || true
    if [ -z "$pids" ]; then
      log "$label: not running"
      return
    fi
    for wp in $pids; do
      taskkill //F //T //PID "$wp" >/dev/null 2>&1 || true
    done
    log "$label stopped"
  }
  stop_component 'manage.py runserver' 'Django backend'
  stop_component 'simulate_farm' 'farm simulator'
  stop_component ' -A config worker' 'celery worker'
  stop_component ' -A config beat' 'celery beat'
  stop_component 'morghdari.subscriber' 'MQTT subscriber'
  rm -f "$BACKEND"/demo-stack-*.pid
  log "backend + simulator + subscriber + celery worker/beat stopped (EMQX container kept — 'docker stop $EMQX_CONTAINER' to remove)"
  exit 0
fi

SIM_ARGS=()
[ "${1:-}" = "--calm" ] && SIM_ARGS+=(--scenario calm)

# ── 1. EMQX broker ──────────────────────────────────────────────────────────
# Run with the FULL config inline (image emqx:5.5 — the compose file's tag):
# dashboard password, the built-in-DB authn chain, deny-by-default
# authorization, and the management-API key bootstrap (EMQX 5.5's /api/v5
# requires API keys; scripts/emqx_provision.py and the stack need it).
# MSYS_NO_PATHCONV=1 stops Git-Bash mangling the container-side mount path
# into a Windows path. A container that predates the API-key bootstrap is
# recreated — the broker keeps no state worth keeping (data lives in
# Postgres/Redis).
run_emqx() {
  MSYS_NO_PATHCONV=1 docker run -d --name "$EMQX_CONTAINER" \
    -p 1883:1883 -p 8883:8883 -p 18083:18083 \
    -e EMQX_DASHBOARD__DEFAULT_PASSWORD=dev_password \
    -e EMQX_DASHBOARD__LISTENERS__HTTP__AUTH_TYPE=basic \
    -e EMQX_AUTHENTICATION__1__MECHANISM=password_based \
    -e EMQX_AUTHENTICATION__1__BACKEND=built_in_database \
    -e EMQX_AUTHORIZATION__NO_MATCH=deny \
    -e EMQX_API_KEY__BOOTSTRAP_FILE=/opt/emqx/etc/api_keys \
    -v poultry_the_main_by_ai_emqx_dev_data:/opt/emqx/data \
    -v "$ROOT/scripts/emqx-api-keys.txt:/opt/emqx/etc/api_keys:ro" \
    emqx:5.5
}
if docker ps --format '{{.Names}}' | grep -qx "$EMQX_CONTAINER"; then
  if docker inspect "$EMQX_CONTAINER" --format '{{range .Config.Env}}{{.}} {{end}}' \
      | grep -q 'EMQX_API_KEY__BOOTSTRAP_FILE'; then
    log "EMQX container '$EMQX_CONTAINER' already running (configured)"
  else
    log "EMQX container predates the API-key bootstrap — recreating"
    docker rm -f "$EMQX_CONTAINER"
    run_emqx
  fi
elif docker ps -a --format '{{.Names}}' | grep -qx "$EMQX_CONTAINER"; then
  log "recreating stopped EMQX container"
  docker rm -f "$EMQX_CONTAINER"
  run_emqx
else
  log "creating EMQX container (MQTT :1883, dashboard :18083)"
  run_emqx
fi
# Wait for the broker dashboard to answer before anything depends on it.
for i in $(seq 1 30); do
  if curl -s -o /dev/null --max-time 2 http://127.0.0.1:18083/api/v5/status; then
    log "EMQX dashboard answering on :18083"
    break
  fi
  [ "$i" = 30 ] && { log "WARNING: EMQX dashboard not answering yet (continuing anyway)"; }
  sleep 1
done

# ── 2. seed demo farm (idempotent) + fresh manifest ─────────────────────────
log "seeding demo farm (idempotent)"
(cd "$BACKEND" && DATABASE_URL="$DB_URL" PYTHONUNBUFFERED=1 \
  "$PY" manage.py demo_farm_seed --manifest-out "$(basename "$MANIFEST")")
log "restarting simulator (fresh scenario clock; backend data NOT deleted)"

# ── 2.5 EMQX authn + topic ACLs ────────────────────────────────────────────
# Per-farm simulator users (may publish ONLY their own farm's topics) plus
# the subscribe-only subscriber user — idempotent, and run on EVERY start so
# a recreated container is re-secured automatically.
log "provisioning EMQX authn + topic ACLs (per-farm sim users, subscriber)"
(cd "$BACKEND" && "$PY" "$ROOT/scripts/emqx_provision.py" --manifest "$MANIFEST")

# ── 3. Django backend on :8000 ───────────────────────────────────────────────
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/api/docs/; then
  log "backend already answering on :8000 (if it predates your changes, restart with --stop)"
else
  log "starting Django backend on :8000 (log: backend/demo-server.log)"
  (cd "$BACKEND" && DATABASE_URL="$DB_URL" PYTHONUNBUFFERED=1 \
    nohup "$PY" manage.py runserver 0.0.0.0:8000 > demo-server.log 2>&1 &
  echo $! > "$BACKEND/demo-stack-backend.pid")
  for i in $(seq 1 30); do
    curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/api/docs/ && break
    [ "$i" = 30 ] && { log "ERROR: backend did not come up — see backend/demo-server.log"; exit 1; }
    sleep 1
  done
fi

# ── 3.5 clear stale dedup keys (fresh sim timeline) ─────────────────────────
# The demo sim fast-forwards device_ts (1 min per 5 s tick); after hours of
# running, day-old dedup keys (TTL 25 h) cover every timestamp and new
# publishes are silently dropped as duplicates — the stream freezes with no
# DLQ. Always clear before (re)starting the sim.
log "clearing stale dedup keys (fresh sim timeline)"
(cd "$BACKEND" && DATABASE_URL="$DB_URL" "$PY" -c "
import redis
r = redis.Redis.from_url('redis://:dev_redis_password@localhost:6379', decode_responses=True)
n = 0
for k in r.scan_iter('dedup:*', count=500):
    r.delete(k); n += 1
print('cleared %d stale dedup keys' % n)
")

# ── 4. farm simulator (the data source) ─────────────────────────────────────
if [ -f "$BACKEND/demo-stack-sim.pid" ] && kill -0 "$(cat "$BACKEND/demo-stack-sim.pid")" 2>/dev/null; then
  log "simulator already running (pid $(cat "$BACKEND/demo-stack-sim.pid"))"
else
  log "starting farm simulator (interval 5s; log: backend/demo-sim.log)"
  (cd "$BACKEND" && PYTHONUNBUFFERED=1 \
    nohup "$PY" "$ROOT/scripts/simulate_farm.py" --manifest "$MANIFEST" --interval 5 \
    --mqtt-username-prefix demo-sim --mqtt-password devpass \
    "${SIM_ARGS[@]}" > demo-sim.log 2>&1 &
  echo $! > "$BACKEND/demo-stack-sim.pid")
fi

# ── 5. celery worker (executes the drain tasks the beat kicks) ─────────────
if [ -f "$BACKEND/demo-stack-worker.pid" ] && kill -0 "$(cat "$BACKEND/demo-stack-worker.pid")" 2>/dev/null; then
  log "celery worker already running"
else
  log "starting celery worker (drains streams into Postgres; log: backend/celery-worker.log)"
  (cd "$BACKEND" && DATABASE_URL="$DB_URL" REDIS_URL="redis://:dev_redis_password@localhost:6379" && PYTHONUNBUFFERED=1 \
    nohup "$PY" -m celery -A config worker -l info -P solo -Q telemetry_realtime,telemetry_historical > celery-worker.log 2>&1 &
  echo $! > "$BACKEND/demo-stack-worker.pid")
fi

# ── 6. celery beat (drain tasks are beat-kicked every 10s) ──────────────────
if [ -f "$BACKEND/demo-stack-beat.pid" ] && kill -0 "$(cat "$BACKEND/demo-stack-beat.pid")" 2>/dev/null; then
  log "celery beat already running"
else
  log "starting celery beat (drains streams into Postgres every 10s)"
  (cd "$BACKEND" && DATABASE_URL="$DB_URL" REDIS_URL="redis://:dev_redis_password@localhost:6379" &&    nohup "$PY" -m celery -A config beat -l info > celery-beat.log 2>&1 &
  echo $! > "$BACKEND/demo-stack-beat.pid")
fi

# ── 7. MQTT subscriber (stream ingress; stable client id + demo creds) ─────
# Canonical env from demo_live.bat: demo-subscriber-01 / demo-sub / devpass.
# demo-sub is the subscribe-only user provisioned by emqx_provision.py. The
# stable client id is REQUIRED (persistent session — broker-side queue);
# launching without it or with missing credentials caused the duplicate
# subscribers and CONNACK failures this block exists to prevent.
if [ -f "$BACKEND/demo-stack-subscriber.pid" ] && kill -0 "$(cat "$BACKEND/demo-stack-subscriber.pid")" 2>/dev/null; then
  log "MQTT subscriber already running"
else
  log "starting MQTT subscriber (demo-subscriber-01; log: backend/demo-subscriber.log)"
  (cd "$BACKEND" && DATABASE_URL="$DB_URL" REDIS_URL="redis://:dev_redis_password@localhost:6379" \
    SUBSCRIBER_CLIENT_ID="demo-subscriber-01" MQTT_USERNAME="demo-sub" MQTT_PASSWORD="devpass" PYTHONUNBUFFERED=1 \
    nohup "$PY" -m morghdari.subscriber > demo-subscriber.log 2>&1 &
  echo $! > "$BACKEND/demo-stack-subscriber.pid")
fi

log "stack is up:"
log "  dashboard  http://127.0.0.1:8000/demo/          (login demo-owner / demo-pass-123)"
log "  console    http://127.0.0.1:8000/demo/console/  (engineering console)"
log "  EMQX       http://127.0.0.1:18083/              (broker dashboard)"
log "  subscriber demo-subscriber-01 (demo-sub/devpass) log: backend/demo-subscriber.log"
log "  stop with: bash scripts/start_demo_stack.sh --stop"
