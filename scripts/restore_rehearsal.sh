#!/usr/bin/env bash
# M7-05 — restore rehearsal (Deployment Guide §5.2 / DR §3.1).
# Stops writers → drops → CREATE EXTENSION → pg_restore → verifies hypertables
# → restarts. Run with the dump path as $1. Logs elapsed time (the DR drill log
# wants RTO evidence, not just success).
#
#   REHEARSAL on the laptop:  scripts/restore_rehearsal.sh backups/postgres/morghdari_YYYYMMDD.dump
#   VPS day:                  sudo scripts/restore_rehearsal.sh /backups/postgres/morghdari_YYYYMMDD.dump

set -euo pipefail

DUMP="${1:?usage: restore_rehearsal.sh <path/to/morghdari_YYYYMMDD.dump>}"
CONTAINER="${CONTAINER:-morghdari-postgres}"
DB_NAME="${DB_NAME:-morghdari}"
ADMIN_USER="${ADMIN_USER:-morghdari_admin}"
APP_ROLE="${APP_ROLE:-morghdari_app}"
COMPOSE_FILE="${COMPOSE_FILE:--f docker-compose.prod.yml}"
PROJECT="${PROJECT:--p morghdari}"

START=$(date +%s)

echo "== 1. stop writers"
docker compose $PROJECT $COMPOSE_FILE stop daphne celery-worker celery-beat subscriber-01 subscriber-02

echo "== 2. drop + recreate (as ADMIN — the app role cannot DROP/CREATE)"
docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d postgres -c "DROP DATABASE IF EXISTS $DB_NAME;"
docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d postgres -c "CREATE DATABASE $DB_NAME;"

echo "== 3. TimescaleDB extension BEFORE pg_restore (superuser-only DDL)"
docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo "== 4. pg_restore (custom-format; roles re-pointed to the app role)"
docker exec -i "$CONTAINER" pg_restore -U "$ADMIN_USER" -d "$DB_NAME" \
  --no-owner --role="$APP_ROLE" < "$DUMP"

echo "== 5. verify hypertables"
docker exec -i "$CONTAINER" psql -U "$ADMIN_USER" -d "$DB_NAME" -c \
  "SELECT hypertable_name, num_dimensions FROM timescaledb_information.hypertables;"

echo "== 6. restart services"
docker compose $PROJECT $COMPOSE_FILE up -d

END=$(date +%s)
echo "== restore drill complete in $((END - START))s (log this as RTO evidence)"