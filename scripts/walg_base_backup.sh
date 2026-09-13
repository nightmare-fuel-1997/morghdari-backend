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

CONTAINER="${CONTAINER:-morghdari-postgres}"

echo "== 1. wal-g base backup (backup-push)"
docker exec -i "$CONTAINER" wal-g backup-push

echo "== 2. evidence — backups on the archive (wal-g backup-list)"
docker exec -i "$CONTAINER" wal-g backup-list

echo "wal-g base backup complete"