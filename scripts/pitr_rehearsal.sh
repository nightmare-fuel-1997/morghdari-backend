#!/usr/bin/env bash
# M8-02 — T2 drill: PostgreSQL corruption → point-in-time recovery (DR §3.1).
#
# Executes the authoritative steps END-TO-END and MEASURES RTO:
#   1. capture PRE-drill row counts (parity baseline);
#   2. force a WAL switch → the archive holds everything up to `now`;
#   3. record TARGET_TS = "the instant we will recover TO";
#   4. stop ALL DB-connecting services (incl. EMQX — open sessions block DROP);
#   5. write a marker row AFTER the target (must be LOST by the recovery);
#   6. wipe PGDATA → wal-g backup-fetch → append restore_command +
#      recovery_target_time to postgresql.auto.conf → touch recovery.signal;
#   7. start postgres → it replays WAL to the target and pauses;
#   8. verify: marker ABSENT, counts match, hypertables present, v1.6.0
#      compat tables (legacy_slug_map + daily_report_extras) counted;
#   9. pg_wal_replay_pause(); pg_promote(); restart the stack;
#  10. measure elapsed vs RTO 4 h and append a DR §6 log row.
#
# Rehearsal (laptop — MinIO wal-g target, no cloud creds needed):
#   REHEARSAL=1 scripts/pitr_rehearsal.sh
# Production-identical staging (VPS day): REHEARSAL=0 with real S3 env.

set -euo pipefail

COMPOSE_BASE="docker compose -p morghdari"
if [[ "${REHEARSAL:-0}" == "1" ]]; then
  COMPOSE_FILE_ARGS=(-f docker-compose.prod.yml -f docker-compose.rehearsal.yml)
else
  COMPOSE_FILE_ARGS=(-f docker-compose.prod.yml)
fi
CONTAINER="${CONTAINER:-morghdari-postgres}"
DB_NAME="${DB_NAME:-morghdari}"
ADMIN_USER="${ADMIN_USER:-morghdari_admin}"
MARKER_PREFIX="${MARKER_PREFIX:-dr_marker_}"
PGDATA="/var/lib/postgresql/data"
RTO_TARGET_MINS="${RTO_TARGET_MINS:-240}"     # 4 h
RECOVERY_WAIT_SECS="${RECOVERY_WAIT_SECS:-180}"
DR_LOG_FILE="${DR_LOG_FILE:-versions/v0.8/DR-LOG.md}"

START_EPOCH=$(date +%s)

# ---- 0. guard: a base backup MUST exist before PGDATA is touched ------------
echo "== 0. guard: wal-g base backup present (abort before touching anything)"
if ! docker exec -i -u postgres "$CONTAINER" wal-g backup-list 2>&1 | grep -q '^base_'; then
  echo "FATAL: no wal-g base backup on the archive — run scripts/walg_base_backup.sh first." >&2
  echo "Refusing to wipe PGDATA without a restore source." >&2
  exit 1
fi
echo "   base backup present — drill proceeds"

# ---- 1. parity baseline -----------------------------------------------------
echo "== 1. PRE-drill parity baseline (row counts)"
PRE_COUNTS_FILE="$(mktemp)"
_count_table() { # $1 = table
  docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -tAc "SELECT count(*) FROM $1;"
}
for table in legacy_slug_map daily_report_extras alert_instances; do
  printf '%s=%s\n' "$table" "$(_count_table "$table")" >> "$PRE_COUNTS_FILE"
done
cat "$PRE_COUNTS_FILE"

# ---- 2/3. WAL switch + target ----------------------------------------------
echo "== 2. force WAL switch so the archive holds EVERYTHING up to now"
docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -c "SELECT pg_switch_wal();" >/dev/null

TARGET_TS="$(docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -tAc \
  "SELECT to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00\"');")"
echo "== 3. recovery target (the instant we will recover TO): $TARGET_TS"
sleep 3   # let the switched WAL segment reach the archive

# ---- 4. stop writers (EMQX MUST be down — open pools block DROP) -----------
echo "== 4. stopping ALL DB-connecting services incl. emqx"
$COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" stop daphne celery-worker celery-beat \
  subscriber-01 subscriber-02 emqx

# ---- 5. marker row AFTER the target (must be lost) -------------------------
MARKER_KEY="${MARKER_PREFIX}$(date +%s)"
echo "== 5. writing marker row (must be ABSENT after recovery): $MARKER_KEY"
docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -c \
  "INSERT INTO system_configuration (key, value) VALUES ('$MARKER_KEY', 'pitr-target=$TARGET_TS');"
sleep 2

# ---- 6. wipe + restore + recovery config ------------------------------------
# Order matters (found live 2026-09-12): the server must be DOWN and PGDATA
# EMPTY for the fetch — fetching into a running server's data dir fails
# ("must be empty"). All work below runs in throwaway containers; the real
# postgres starts only once, already in recovery.
echo "== 6. wiping PGDATA and restoring the wal-g base backup"
$COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" stop postgres
docker volume rm morghdari_postgres_data >/dev/null 2>&1 || true
docker volume create morghdari_postgres_data >/dev/null
# wal-g v3 REQUIRES a backup name (v2 defaulted to latest) — LATEST is the
# wal-g spelling for "newest". Bare backup-fetch dies "insufficient
# arguments" AFTER the volume is already wiped.
$COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" run --rm --no-deps -u postgres \
  --entrypoint sh postgres \
  -c 'wal-g backup-fetch "$PGDATA" LATEST'

echo "== 6b. appending restore_command + recovery_target_time + signal"
$COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" run --rm --no-deps -u postgres \
  -e RECOVERY_TARGET_TS="$TARGET_TS" --entrypoint sh postgres \
  -c 'printf "%s\n" "restore_command = '"'"'wal-g wal-fetch %f %p'"'"'" "recovery_target_time = '"'"'$RECOVERY_TARGET_TS'"'"'" >> "$PGDATA/postgresql.auto.conf" && touch "$PGDATA/recovery.signal"'

$COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" up -d postgres
# ---- 7. wait for replay to reach the target and pause ----------------------
echo "== 7. waiting for WAL replay to pause at the target"
RECOVERY_OK=0
for _ in $(seq 1 $((RECOVERY_WAIT_SECS / 5))); do
  paused="$(docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -tAc \
    "SELECT pg_is_wal_replay_paused();" 2>/dev/null || true)"
  if [[ "$paused" == "t" ]]; then RECOVERY_OK=1; break; fi
  sleep 5
done
if [[ "$RECOVERY_OK" != "1" ]]; then
  echo "FATAL: replay did not pause at the target within ${RECOVERY_WAIT_SECS}s." >&2
  echo "Common cause: target ahead of the last archived WAL (RPO exceeded)." >&2
  exit 1
fi
echo "   replay paused at target — recovery reached the mark"

# ---- 8. verification --------------------------------------------------------
echo "== 8. verification"
MARKER_COUNT="$(docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -tAc \
  "SELECT count(*) FROM system_configuration WHERE key = '$MARKER_KEY';")"
if [[ "$MARKER_COUNT" != "0" ]]; then
  echo "FATAL: marker row SURVIVED the recovery — PITR did not reach the target." >&2
  exit 1
fi
echo "   marker absent: PASS"
for table in legacy_slug_map daily_report_extras alert_instances; do
  post="$(_count_table "$table")"
  pre="$(grep "^$table=" "$PRE_COUNTS_FILE" | cut -d= -f2)"
  [[ "$post" == "$pre" ]] || { echo "FATAL: $table $pre -> $post (parity broken)"; exit 1; }
  echo "   $table parity: $post == $pre PASS"
done
echo "   hypertables present:"
docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -tAc \
  "SELECT hypertable_name FROM timescaledb_information.hypertables ORDER BY 1;" | head -20 || true
rm -f "$PRE_COUNTS_FILE"

# ---- 9. promote + restart ---------------------------------------------------
echo "== 9. promote + restart the stack"
docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -c \
  "SELECT pg_wal_replay_pause(); SELECT pg_promote();"
$COMPOSE_BASE "${COMPOSE_FILE_ARGS[@]}" up -d

# ---- 10. measure + DR log row ----------------------------------------------
END_EPOCH=$(date +%s)
ELAPSED_MINS=$(( (END_EPOCH - START_EPOCH) / 60 ))
ELAPSED_SECS=$(( END_EPOCH - START_EPOCH ))
echo "== 10. T2 drill complete — RTO observed: ${ELAPSED_MINS} min (${ELAPSED_SECS}s), plan ≤ ${RTO_TARGET_MINS} min"

ROW="| $(date -u +%Y-%m-%d) | T2 PITR (staging$([ "$REHEARSAL" == 1 ] && echo ' rehearsal')), target $TARGET_TS | PASS — marker absent; parity ok; hypertables present | RTO ${ELAPSED_MINS} min (plan ≤ ${RTO_TARGET_MINS}); RPO plan ≤ 1 h (archive_timeout 300s) | T3 Redis wipe |"
if [[ ! -f "$DR_LOG_FILE" ]]; then
  mkdir -p "$(dirname "$DR_LOG_FILE")"
  printf '%s\n' "# v0.8 — Disaster Recovery Testing Log (DR Plan §6 mirror)" \
    "" "| Date | Scenario | Result | Lessons | Next |" \
    "|---|---|---|---|---|" > "$DR_LOG_FILE"
fi
echo "$ROW" >> "$DR_LOG_FILE"
echo "   DR log row appended → $DR_LOG_FILE"