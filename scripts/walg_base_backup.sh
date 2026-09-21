#!/usr/bin/env bash
# M8-01 — wal-g base backup + WAL continuity evidence (DR §3.3).
#
# Runs INSIDE the postgres-walg container: the wal-g binary AND its S3
# storage env (WALG_S3_PREFIX / AWS_*) live on that service (compose-wired).
# The base backup is the T2 drill's starting point; WAL continuity is the
# separate DR §4 daily check (last archived segment < 1 h old).
#
# VPS crontab (root):
#   0 3 * * 0 /opt/morghdari/scripts/walg_base_backup.sh >> /var/log/morghdari/walg.log 2>&1
# Rehearsal (laptop — MinIO target):
#   REHEARSAL=1 scripts/walg_base_backup.sh

set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER="${CONTAINER:-morghdari-postgres}"

# BUG-15 (QA 19.3): as committed, wal-g connected as the OS user inside the
# container (root) -> FATAL: role "root" does not exist. wal-g needs the
# admin role explicitly. Values come from the environment or the repo .env;
# the password is mandatory.
# فارسی: اعتبارنامهٔ نقشِ ادمین برای wal-g الزامی است؛ وگرنه اتصال با کاربر
# root شکست میخورد.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
: "${PGUSER:=${POSTGRES_ADMIN_USER:-morghdari_admin}}"
: "${PGPASSWORD:=${POSTGRES_ADMIN_PASSWORD:-}}"
: "${PGDATABASE:=${POSTGRES_DB:-${DATABASE_NAME:-morghdari}}}"
if [[ -z "$PGPASSWORD" ]]; then
  echo "!! wal-g needs PGPASSWORD (or POSTGRES_ADMIN_PASSWORD in .env)" >&2
  exit 1
fi

echo "== 1. wal-g base backup (backup-push)"
docker exec -i -e PGUSER="$PGUSER" -e PGPASSWORD="$PGPASSWORD" \
  -e PGDATABASE="$PGDATABASE" "$CONTAINER" wal-g backup-push

echo "== 2. evidence — backups on the archive (wal-g backup-list)"
docker exec -i -e PGUSER="$PGUSER" -e PGPASSWORD="$PGPASSWORD" \
  -e PGDATABASE="$PGDATABASE" "$CONTAINER" wal-g backup-list

echo "wal-g base backup complete"
