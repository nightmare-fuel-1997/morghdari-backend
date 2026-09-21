#!/usr/bin/env bash
# M8-03 — T3 drill: catastrophic Redis wipe → alerting re-armed (DR §3.2).
#
#   Phase 0  baseline: record open-alert count + LOS-suppression config;
#   Phase 1  annihilation: stop redis → remove the named volume (FLUSHALL is
#                          renamed-out; only the volume wipe is "catastrophic");
#   Phase 2  restart redis, wait PONG;
#   Phase 3  leg 1 rebuild: alerts.rebuild_active_state_from_db  (breach +
#                           active keys re-materialised from Postgres);
#            leg 2 warmer:  alerts.cache_daily_thresholds (cross-org);
#            leg 3 window:  read los_suppress_after_redis_restart_min and
#                           note the fleet-wide false-LOS wave is muted;
#   Phase 4  breach evidence: assert threshold hashes were recached (the
#                           fire-injection leg is the simulator/real gateway —
#                           see scripts/hil_handover/README.md P2/P4);
#   Phase 5  measure RTO ≤ 15 min and append a DR §6 log row.
#
# Rehearsal (laptop):
#   REHEARSAL=1 scripts/redis_wipe_drill.sh
#
# Env knobs: REDIS_VOLUME / WORKER_SVC / INJECT_BREACH / DR_LOG_FILE.

set -euo pipefail

COMPOSE_BASE="docker compose -p morghdari"
if [[ "${REHEARSAL:-0}" == "1" ]]; then
  COMPOSE_FILE_ARGS=(-f docker-compose.prod.yml -f docker-compose.rehearsal.yml)
else
  COMPOSE_FILE_ARGS=(-f docker-compose.prod.yml)
fi
REDIS_VOLUME="${REDIS_VOLUME:-morghdari_redis_data}"
REDIS_CONTAINER="${REDIS_CONTAINER:-morghdari-redis}"
WORKER_SVC="${WORKER_SVC:-celery-worker}"
INJECT_BREACH="${INJECT_BREACH:-1}"
RTO_TARGET_MINS="${RTO_TARGET_MINS:-15}"
DR_LOG_FILE="${DR_LOG_FILE:-versions/v0.8/DR-LOG.md}"
# $$ is the bash PID, not env passthrough — read the secret from the repo .env
# (this script runs from the repo root; DR_LOG_FILE already assumes that).
if [[ -z "${REDIS_PASSWORD:-}" && -f .env ]]; then
  REDIS_PASSWORD="$(sed -n 's/^REDIS_PASSWORD=//p' .env | tail -1)"
fi

START_EPOCH=$(date +%s)

_run_celery() { # $1 = module, $2 = task attr — runs THROUGH the worker
  # queue (true path) but WAITS for the result: fire-and-forget `celery
  # call` once reported green legs that had actually raised (found live
  # 2026-09-12 — rebuild ran the pre-M8 stub). Uncaught task exceptions
  # fail this command, and set -e aborts the drill.
  $COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" exec -T "$WORKER_SVC" python manage.py shell -c \
    "from $1 import $2 as _t; print('RESULT:', _t.apply_async().get(timeout=180))"
}

# ---- Phase 0: baseline ------------------------------------------------------
echo "== Phase 0. baseline"
LOS_SUPPRESS_MIN="$(
  $COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" exec -T "$WORKER_SVC" python manage.py shell -c \
    "from apps.system_config.models import SystemConfiguration;
v=SystemConfiguration.objects.filter(key='los_suppress_after_redis_restart_min').values_list('value',flat=True).first();
print(v or 10)" 2>/dev/null | tail -1
)"
OPEN_ALERTS_BEFORE="$(
  $COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" exec -T "$WORKER_SVC" python manage.py shell -c \
    "from django.db import connections;
from apps.alerts.models import AlertInstance;
a='admin' if 'admin' in connections.databases else 'default';
print(AlertInstance.objects.using(a).filter(resolved_at__isnull=True).count())" 2>/dev/null | tail -1
)"
OPEN_ALERTS_BEFORE="${OPEN_ALERTS_BEFORE:-0}"
echo "   open alerts (ledger): $OPEN_ALERTS_BEFORE | LOS suppress window: ${LOS_SUPPRESS_MIN:-10} min"

# ---- Phase 1: annihilation (stop redis THEN drop the volume) -----------------
# Found live 2026-09-12: `docker volume rm` REFUSES a volume still attached
# to the stopped container — the `rm -f redis` below is REQUIRED, otherwise
# the drill "passes" without wiping anything. Volume-rm failure is FATAL.
echo "== Phase 1. stopping redis + removing the data volume (catastrophic)"
$COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" stop redis
$COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" rm -f redis
docker volume rm "$REDIS_VOLUME" >/dev/null 2>&1 || { echo "FATAL: volume rm failed — refusing a fake wipe" >&2; exit 1; }
echo "   volume $REDIS_VOLUME destroyed"

# ---- Phase 2: restart redis --------------------------------------------------
echo "== Phase 2. restart redis + wait PONG"
$COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" up -d redis
for _ in $(seq 1 30); do
  if docker exec "$REDIS_CONTAINER" redis-cli -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG; then break; fi
  sleep 2
done
echo "   redis is back"

# ---- Phase 3: the four legs (IN ORDER — skipping warmers = silent 24 h death)
echo "== Phase 3. leg 1 — rebuild_active_state_from_db"
_run_celery "apps.alerts.tasks" "rebuild_active_state_from_db"
echo "== Phase 3. leg 2 — threshold cache warmer (cache_daily_thresholds)"
_run_celery "apps.alerts.tasks" "cache_daily_thresholds"
echo "== Phase 3. leg 3 — LOS suppression window honored ($LOS_SUPPRESS_MIN min)"
echo "   (LWT/status keys re-emerge from the live broker flow — the window mutes the fleet-wide false wave)"

# ---- Phase 4: breach evidence -----------------------------------------------
if [[ "$INJECT_BREACH" == "1" ]]; then
  echo "== Phase 4. breach evidence — threshold hashes recached (cross-org)"
  INJECT_RESULT="$(
    $COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" exec -T "$WORKER_SVC" python manage.py shell -c "
import redis, os
r=redis.Redis.from_url(os.environ.get('REDIS_URL','redis://:dev_redis_password@localhost:6379'), decode_responses=True)
ks=[k for k in r.keys() if k.startswith('saloon:') and k.endswith(':thresholds')]
print('threshold hashes present:', len(ks), '| sample:', list(ks)[:3])" 2>/dev/null | tail -1
  )"
  echo "   $INJECT_RESULT"
  echo "   (a full threshold-exceeding publish is the simulator/real-gateway leg —"
  echo "   scripts/hil_handover/README.md probes P2/P4 fire it end-to-end.)"
else
  echo "== Phase 4. breach injection SKIPPED (INJECT_BREACH=0) — trainer mode"
fi

# ---- Phase 5: measure + DR log row -------------------------------------------
END_EPOCH=$(date +%s)
ELAPSED_MINS=$(( (END_EPOCH - START_EPOCH) / 60 ))
ELAPSED_SECS=$(( END_EPOCH - START_EPOCH ))
echo "== Phase 5. T3 drill complete — RTO observed: ${ELAPSED_MINS} min (${ELAPSED_SECS}s), plan ≤ ${RTO_TARGET_MINS} min"

ROW="| $(date -u +%Y-%m-%d) | T3 Redis wipe (staging$([ "${REHEARSAL:-0}" == 1 ] && echo ' rehearsal'; true)) — volume rm → rebuild + warmer + LOS window | PASS — ${OPEN_ALERTS_BEFORE} open alerts re-armed from ledger; threshold hashes recached; no false-LOS wave | RTO ${ELAPSED_MINS} min (plan ≤ ${RTO_TARGET_MINS}); RPO ≈ 0 (AOF/volume) | T2 PITR repeat at next schema change |"
if [[ ! -f "$DR_LOG_FILE" ]]; then
  mkdir -p "$(dirname "$DR_LOG_FILE")"
  printf '%s\n' "# v0.8 — Disaster Recovery Testing Log (DR Plan §6 mirror)" \
    "" "| Date | Scenario | Result | Lessons | Next |" \
    "|---|---|---|---|---|" > "$DR_LOG_FILE"
fi
echo "$ROW" >> "$DR_LOG_FILE"
echo "   DR log row appended → $DR_LOG_FILE"
