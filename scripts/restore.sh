#!/usr/bin/env bash
# restore.sh — restore a pg_dump -Fc artifact into a database, CORRECTLY.
#
#   bash scripts/restore.sh <dumpfile> <target-db> [--force]
#
# WHY THIS SCRIPT EXISTS (2026-10-24, restore drill):
#   The obvious command — `pg_restore -d <db> dump` — restores this database
#   *approximately*. saloon_telemetry and metric_hourly_aggregates are Timescale
#   hypertables; restoring them without the Timescale hooks leaves the chunk
#   constraints unapplied and exits 1 with ~27 errors of the form
#   "ONLY option not supported on hypertable operations". Data lands, the
#   hypertables come back, and nothing tells you the integrity constraints are
#   missing — the worst possible failure mode for a disaster recovery.
#
#   The canonical sequence (Timescale docs) is:
#     CREATE EXTENSION IF NOT EXISTS timescaledb;
#     SELECT timescaledb_pre_restore();
#     pg_restore ...
#     SELECT timescaledb_post_restore();
#   Drilled 2026-10-24: that sequence gives pg_restore exit 0 and ZERO errors,
#   with both hypertables and their chunks intact.
#
# SAFETY: refuses to write into the live database unless --force is given, and
# refuses to start if the dump is missing or unreadable. A restore is a
# destructive, deliberate operation — never a side effect.
#
# VPS example (drill into a scratch DB — the recommended way to verify a backup):
#   bash scripts/restore.sh /backups/postgres/morghdari_20261024.dump \
#        morghdari_restore_drill
#
# Real disaster recovery (after the stack is up and the DB is EMPTY):
#   bash scripts/restore.sh /backups/postgres/<dump> morghdari --force

set -uo pipefail

DUMP="${1:-}"
TARGET="${2:-}"
FORCE="${3:-}"

if [[ -z "$DUMP" || -z "$TARGET" ]]; then
  echo "usage: restore.sh <dumpfile> <target-db> [--force]" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
[[ -f .env ]] && { set -a; . ./.env; set +a; }

CONTAINER="${CONTAINER:-morghdari-postgres}"
ADMIN_USER="${ADMIN_USER:-${POSTGRES_ADMIN_USER:-morghdari_admin}}"
LIVE_DB="${DB_NAME:-${DATABASE_NAME:-morghdari}}"

die() { echo "FATAL: $*" >&2; exit 1; }

[[ -f "$DUMP" ]] || die "dump not found: $DUMP"
[[ -s "$DUMP" ]] || die "dump is empty: $DUMP"

# The archive must be readable as a pg_dump custom-format file before we touch
# anything. `pg_restore -l` fails loudly on a truncated/corrupt artifact.
docker exec -i "$CONTAINER" pg_restore -l < "$DUMP" >/dev/null 2>&1 \
  || die "not a readable pg_dump archive: $DUMP"

if [[ "$TARGET" == "$LIVE_DB" && "$FORCE" != "--force" ]]; then
  cat >&2 <<EOF
FATAL: refusing to restore into the LIVE database ($LIVE_DB) without --force.

  A restore overwrites data. Drill into a scratch database instead:
    bash scripts/restore.sh $DUMP ${LIVE_DB}_restore_drill
  Real disaster recovery (DB is empty, stack already up):
    bash scripts/restore.sh $DUMP $LIVE_DB --force
EOF
  exit 3
fi

echo "==> restore $DUMP  ->  database '$TARGET' (container $CONTAINER, user $ADMIN_USER)"

# 1. fresh target
if docker exec "$CONTAINER" psql -U "$ADMIN_USER" -d postgres -tAc \
     "SELECT 1 FROM pg_database WHERE datname='$TARGET';" | grep -q 1; then
  [[ "$FORCE" == "--force" ]] || die "database '$TARGET' already exists (use --force to drop it)"
  echo "==> dropping existing '$TARGET' (--force)"
  docker exec "$CONTAINER" psql -U "$ADMIN_USER" -d postgres -c "DROP DATABASE \"$TARGET\";" >/dev/null \
    || die "could not drop $TARGET"
fi
docker exec "$CONTAINER" psql -U "$ADMIN_USER" -d postgres -c "CREATE DATABASE \"$TARGET\";" >/dev/null \
  || die "could not create $TARGET"

# 2. Timescale preconditions — the whole point of this script
docker exec "$CONTAINER" psql -U "$ADMIN_USER" -d "$TARGET" -q \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" \
  || die "could not create the timescaledb extension in $TARGET"
docker exec "$CONTAINER" psql -U "$ADMIN_USER" -d "$TARGET" -qtA \
  -c "SELECT timescaledb_pre_restore();" >/dev/null \
  || die "timescaledb_pre_restore() failed — refusing to restore into an unprepared DB"

# 3. restore
echo "==> pg_restore (custom format, --no-owner --no-privileges)"
ERRFILE="$(mktemp)"
docker exec -i "$CONTAINER" pg_restore -U "$ADMIN_USER" -d "$TARGET" \
  --no-owner --no-privileges < "$DUMP" 2>"$ERRFILE"
RC=$?

# 4. always run the post hook — it repairs the catalog state the restore left
docker exec "$CONTAINER" psql -U "$ADMIN_USER" -d "$TARGET" -qtA \
  -c "SELECT timescaledb_post_restore();" >/dev/null \
  || echo "!! timescaledb_post_restore() failed — investigate before trusting this DB" >&2

if [[ $RC -ne 0 ]]; then
  echo "!! pg_restore exited $RC — errors:" >&2
  head -20 "$ERRFILE" >&2
  rm -f "$ERRFILE"
  exit 1
fi
if [[ -s "$ERRFILE" ]]; then
  echo "!! pg_restore exited 0 but wrote warnings:" >&2
  head -10 "$ERRFILE" >&2
fi
rm -f "$ERRFILE"

# 5. prove the result instead of assuming it
echo "==> verification"
docker exec "$CONTAINER" psql -U "$ADMIN_USER" -d "$TARGET" -tA -F'=' <<'SQL'
SELECT 'organizations', count(*) FROM organizations
UNION ALL SELECT 'gateway_slots', count(*) FROM gateway_slots
UNION ALL SELECT 'mqtt_device_credentials', count(*) FROM mqtt_device_credentials
UNION ALL SELECT 'saloon_telemetry', count(*) FROM saloon_telemetry
UNION ALL SELECT 'metric_hourly_aggregates', count(*) FROM metric_hourly_aggregates
UNION ALL SELECT 'alert_instances', count(*) FROM alert_instances
UNION ALL SELECT 'gateway_commands', count(*) FROM gateway_commands
ORDER BY 1;
SQL

HYPER=$(docker exec "$CONTAINER" psql -U "$ADMIN_USER" -d "$TARGET" -tAc \
  "SELECT count(*) FROM timescaledb_information.hypertables;")
CHUNKS=$(docker exec "$CONTAINER" psql -U "$ADMIN_USER" -d "$TARGET" -tAc \
  "SELECT count(*) FROM timescaledb_information.chunks;")

echo "  hypertables=$HYPER chunks=$CHUNKS"
if [[ "$HYPER" -lt 2 ]]; then
  echo "!! expected 2 hypertables (saloon_telemetry, metric_hourly_aggregates), found $HYPER" >&2
  echo "!! this restore is NOT trustworthy" >&2
  exit 1
fi

echo "==> OK: '$TARGET' restored cleanly (pg_restore exit 0, hypertables intact)"
