#!/usr/bin/env bash
# vps_health.sh - one-command health/status check for this deployment.
#
#   sudo bash scripts/vps_health.sh            # full report
#   sudo bash scripts/vps_health.sh --quiet    # only problems + the summary line
#
# Read-only: it starts nothing, changes nothing, and never prints a secret
# (passwords stay inside the shell, only booleans and counts are shown).
# Exit code: 0 = healthy, 1 = at least one problem.
#
# 2026-09-16: written after the VPS-day-1 session on 185.8.175.150, where two
# silent failures (an unreadable EMQX key, a bootstrap that aborted on a fresh
# volume) were only found by hand. Run this after every deploy and renewal.

set -uo pipefail
cd "$(dirname "$0")/.."
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
PROBLEMS=0
R() { printf '   %-34s %s\n' "$1" "$2"; }
PROBLEM() { PROBLEMS=$((PROBLEMS + 1)); printf '   %-34s PROBLEM: %s\n' "$1" "$2"; }
HEAD() { [ "$QUIET" = 1 ] || printf '\n== %s\n' "$1"; }

ENV_FILE=".env"
[ -f "$ENV_FILE" ] || { echo "FATAL: $ENV_FILE not found - run from the repo root." >&2; exit 1; }
set -a; . "./$ENV_FILE"; set +a
: "${DATABASE_NAME:=morghdari}" "${POSTGRES_ADMIN_USER:=morghdari_admin}"
TAG="${MORGHDARI_TAG:-vps-01}"
C="docker compose -p morghdari --env-file $ENV_FILE -f docker-compose.prod.yml -f docker-compose.vps.yml"

HEAD "1. certificate material"
if [ -d certs ] && [ -s certs/fullchain.pem ] && [ -s certs/privkey.pem ]; then
  subj=$(openssl x509 -in certs/fullchain.pem -noout -subject -dates 2>/dev/null | tr '\n' ' ')
  R "certificate" "$subj"
  if [ -f certs/STAGING-SELF-SIGNED.txt ]; then
    R "certificate type" "STAGING self-signed (replace with the real one)"
  fi
  # The EMQX container runs as uid 1000: a 0600 root key breaks every MQTT
  # handshake while the listener still reports "running".
  kperm=$(stat -c '%a %u:%g' certs/privkey.pem 2>/dev/null)
  kgrp=$(stat -c '%g' certs/privkey.pem 2>/dev/null)
  if [ "$kgrp" = "1000" ]; then R "privkey group" "$kperm (readable by the broker)";
  else PROBLEM "privkey group" "$kperm - EMQX (uid 1000) cannot read it; run: chgrp 1000 certs/privkey.pem certs/server.key && chmod 0640 certs/privkey.pem certs/server.key"; fi
  if docker exec morghdari-emqx sh -c 'head -1 /opt/emqx/certs/server.key >/dev/null 2>&1' 2>/dev/null; then
    R "broker can read the key" "yes"
  else
    PROBLEM "broker can read the key" "no (MQTT handshakes will be reset)"
  fi
else
  PROBLEM "certificate" "certs/fullchain.pem or privkey.pem missing - nginx and EMQX cannot serve TLS"
fi

HEAD "2. containers"
expected="morghdari-postgres morghdari-redis morghdari-daphne morghdari-celery-worker morghdari-celery-beat morghdari-nginx morghdari-emqx morghdari-subscriber-01 morghdari-subscriber-02"
for c in $expected; do
  s=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)
  if [ "$s" = "healthy" ]; then R "$c" "healthy";
  elif [ "$s" = "missing" ]; then PROBLEM "$c" "not running";
  else PROBLEM "$c" "$s"; fi
done

HEAD "3. HTTP / API"
code=$(curl -sk -m 8 -o /dev/null -w '%{http_code}' https://127.0.0.1/api/v1/health/ || echo 000)
[ "$code" = "200" ] && R "https://127.0.0.1/api/v1/health/" "200" || PROBLEM "nginx -> api health" "HTTP $code"
code80=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1/healthz || echo 000)
[ "$code80" = "200" ] && R "http://127.0.0.1/healthz" "200" || PROBLEM "nginx healthz" "HTTP $code80"
body=$(curl -sk -m 8 https://127.0.0.1/api/v1/health/ 2>/dev/null | head -c 200)
case "$body" in *'"status":"healthy"'*) R "api status" "healthy";; *) PROBLEM "api status" "unexpected body: ${body:0:80}";; esac
case "$body" in *'"database":{"status":"ok"'*) : ;; *) PROBLEM "database probe" "not ok in /api/v1/health/";; esac
case "$body" in *'"redis":{"status":"ok"'*) : ;; *) PROBLEM "redis probe" "not ok in /api/v1/health/";; esac

HEAD "4. database"
P() { docker exec -e PGPASSWORD="***" morghdari-postgres psql -U "$POSTGRES_ADMIN_USER" -d "$DATABASE_NAME" -tAc "$1" 2>&1; }
t=$(P "select count(*) from information_schema.tables where table_schema='public';")
[ "${t:-0}" -gt 50 ] 2>/dev/null && R "public tables" "$t" || PROBLEM "public tables" "${t:-?} (migrate not finished?)"
p=$(P "select count(*) from pg_policies where schemaname='public';")
[ "${p:-0}" -gt 0 ] 2>/dev/null && R "RLS policies" "$p" || PROBLEM "RLS policies" "${p:-?}"
s=$(P "select count(*) from mqtt_device_credentials where username like 'subscriber%';")
[ "${s:-0}" -ge 2 ] 2>/dev/null && R "subscriber accounts" "$s" || PROBLEM "subscriber accounts" "${s:-?} (run scripts/prod_bootstrap.sh)"
for role in "$POSTGRES_ADMIN_USER"; do
  if docker exec -e PGPASSWORD="***" morghdari-postgres psql -U "$role" -d "$DATABASE_NAME" -tAc 'select 1' >/dev/null 2>&1; then R "admin role login" "ok"; else PROBLEM "admin role login" "failed"; fi
done
if docker exec -e PGPASSWORD="***" morghdari-postgres psql -U "${DATABASE_USER:-morghdari_app}" -d "$DATABASE_NAME" -tAc 'select 1' >/dev/null 2>&1; then R "app role login" "ok"; else PROBLEM "app role login" "failed"; fi

HEAD "5. redis"
if docker exec morghdari-redis redis-cli -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG; then R "redis AUTH + PING" "PONG"; else PROBLEM "redis" "AUTH/PING failed"; fi

HEAD "6. MQTT"
clients=$(docker exec morghdari-emqx emqx ctl clients list 2>/dev/null | grep -c 'connected=true')
[ "${clients:-0}" -ge 2 ] 2>/dev/null && R "connected mqtt clients" "$clients" || PROBLEM "connected mqtt clients" "${clients:-0} (expect >= 2 subscribers)"
if timeout 8 openssl s_client -connect 127.0.0.1:8883 -servername emqx -brief </dev/null 2>&1 | grep -q 'CONNECTION ESTABLISHED'; then
  R "mqtt tls handshake :8883" "ok"
else
  PROBLEM "mqtt tls handshake :8883" "failed - check the certificate key permissions"
fi

HEAD "7. workers"
# The beat heartbeat is the deterministic worker proof: monitoring.beat_heartbeat
# is a @shared_task, so beat schedules it and the WORKER executes it - a fresh key
# means the worker is alive AND consuming (same 300s bound as the alert rule).
hb=$(docker exec morghdari-redis redis-cli -a "$REDIS_PASSWORD" get metrics:beat_heartbeat_ts 2>/dev/null)
if [ -n "$hb" ]; then
  age=$(( $(date +%s) - ${hb%%.*} ))
  if [ "$age" -lt 300 ]; then
    R "beat heartbeat" "age ${age}s (beat and worker both alive)"
  else
    PROBLEM "beat heartbeat" "age ${age}s - beat or worker is stuck"
  fi
else
  PROBLEM "beat heartbeat" "no metrics:beat_heartbeat_ts in redis"
fi
# Informational only: `celery inspect ping` needs a reply round-trip and can
# exceed 10s while the worker is busy (observed 2026-09-16), so it must not be
# the pass/fail signal.
ping_out=$(docker exec morghdari-celery-worker celery -A config inspect ping --timeout 20 2>&1)
if printf '%s' "$ping_out" | grep -q 'pong'; then
  R "worker ping (informational)" "pong"
else
  R "worker ping (informational)" "no reply within 20s - judge by the heartbeat above"
fi

HEAD "8. disk and WAL"
df -h / | awk 'NR==2{printf "   %-34s %s used of %s (%s)\n","disk /",$3,$2,$5}'
use=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "${use:-100}" -lt 85 ] || PROBLEM "disk usage" "${use}% - act now"
wal=$(docker exec morghdari-postgres du -sh /var/lib/postgresql/data/pg_wal 2>/dev/null | awk '{print $1}')
R "pg_wal" "${wal:-unknown}"
if [ -z "${WALG_S3_PREFIX:-}" ]; then
  R "wal archiving" "no target configured (WALG_S3_PREFIX empty) - pg_wal grows and PITR is unavailable"
fi

echo
if [ "$PROBLEMS" -eq 0 ]; then
  echo "HEALTH: ok (0 problems)"
  exit 0
else
  echo "HEALTH: $PROBLEMS problem(s) - see above"
  exit 1
fi
