#!/usr/bin/env bash
# Local base backup + retention (2026-10-28).
#
# WHY THIS EXISTS: scripts/walg_base_backup.sh pushes to S3 via wal-g and
# cannot run until WALG_S3_PREFIX + AWS_* credentials exist. Until then this
# script gives a working LOCAL PITR chain: a weekly physical base backup plus
# the compressed WAL archive that postgres keeps in /backups/wal
# (archive_command: gzip -1 -c). Restore = untar the newest base, then replay
# WAL with a restore_command that gunzips.
#
# The base backup is taken with pg_basebackup (-Ft tar, -z gzip, -Xs streaming
# WAL) so it is consistent without touching the running cluster's load.
#
# VPS crontab (root) — installed 2026-10-28:
#   0 3 * * 0 /opt/morghdari/scripts/base_backup_local.sh >> /var/log/morghdari/base_backup.log 2>&1

set -euo pipefail

CONTAINER="${CONTAINER:-morghdari-postgres}"
BASE_DIR="${BASE_DIR:-/backups/base}"
WAL_DIR="${WAL_DIR:-/backups/wal}"
ADMIN_USER="${ADMIN_USER:-morghdari_admin}"
KEEP_BASE_DAYS="${KEEP_BASE_DAYS:-28}"
KEEP_WAL_DAYS="${KEEP_WAL_DAYS:-30}"

DAY=$(date +%F)
echo "[$(date -Is)] base backup starting → ${BASE_DIR}/${DAY}"

docker exec "$CONTAINER" sh -c \
  "mkdir -p ${BASE_DIR}/${DAY} && chown postgres:postgres ${BASE_DIR}/${DAY} && \
   pg_basebackup -U ${ADMIN_USER} -h 127.0.0.1 -D ${BASE_DIR}/${DAY} -Ft -z -Xs -P"

echo "[$(date -Is)] base backup complete: $(du -sh "${BASE_DIR}/${DAY}" | cut -f1)"

# Retention — a base older than the WAL window is useless; a WAL older than
# every kept base is useless. Keep base 28 d, WAL 30 d (2 d of slack).
find "$BASE_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_BASE_DAYS" -exec rm -rf {} +
find "$WAL_DIR" -type f -mtime +"$KEEP_WAL_DAYS" -delete
echo "[$(date -Is)] retention: base ≤${KEEP_BASE_DAYS}d, wal ≤${KEEP_WAL_DAYS}d"
echo "[$(date -Is)] base backups kept: $(find "$BASE_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l), wal segments: $(find "$WAL_DIR" -type f | wc -l)"
