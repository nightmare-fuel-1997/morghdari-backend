#!/usr/bin/env bash
# M7-05 — daily backup (Deployment Guide §5.1).
# NOTE: `docker exec -i` (NEVER `-t`) — allocating a TTY corrupts the binary
# dump with CRLF line endings.
#
# REHEARSAL mode (laptop, v0.7 M7): BACKUP_DIR-relative artifact only; the S3
# upload runs only when AWS CLI + the bucket are configured (REHEARSAL=1 skips
# it and says so — never silently).
#
# VPS crontab (root):
#   0 2 * * * /opt/morghdari/scripts/backup.sh >> /var/log/morghdari/backup.log 2>&1

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups/postgres}"
TIMESTAMP=$(date +%Y%m%d)
RETENTION_DAYS=30
CONTAINER="${CONTAINER:-morghdari-postgres}"
DB_NAME="${DB_NAME:-morghdari}"
ADMIN_USER="${ADMIN_USER:-morghdari_admin}"
S3_BUCKET="${S3_BUCKET:-s3://morghdari-backups/postgres/}"

mkdir -p "$BACKUP_DIR"

# Single custom-format artifact (includes TimescaleDB catalog metadata).
# R3-fix 2026-08-16: run as the ADMIN user (morghdari_admin = POSTGRES_ADMIN_USER)
# — not morghdari_app, which lacks the catalog access a complete dump needs.
docker exec -i "$CONTAINER" pg_dump -U "$ADMIN_USER" -Fc "$DB_NAME" \
  > "${BACKUP_DIR}/morghdari_${TIMESTAMP}.dump"

if [[ "${REHEARSAL:-0}" == "1" ]]; then
  echo "REHEARSAL=1 — S3 upload skipped (local artifact is the drill target)."
elif command -v aws >/dev/null 2>&1; then
  # Upload the dump offsite (S3-compatible; bucket has default SSE-KMS encryption)
  aws s3 cp "${BACKUP_DIR}/morghdari_${TIMESTAMP}.dump" "$S3_BUCKET"
  # Encryption check hint (monthly ritual — TUTORIAL §4):
  #   aws s3api head-object --bucket morghdari-backups \
  #     --key "postgres/morghdari_${TIMESTAMP}.dump"  →  expect "aws:kms"
else
  echo "WARNING: aws CLI absent and REHEARSAL!=1 — dump is LOCAL-ONLY" >&2
fi

# Local retention
find "$BACKUP_DIR" -name "*.dump" -mtime +"$RETENTION_DAYS" -delete
echo "backup complete: ${BACKUP_DIR}/morghdari_${TIMESTAMP}.dump"
