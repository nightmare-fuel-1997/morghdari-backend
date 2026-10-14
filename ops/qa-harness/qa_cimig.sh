#!/usr/bin/env bash
# Probe: run real migrate verbosely against the CI postgres to find the failing migration.
cd "$(dirname "$0")/.."
docker compose -f docker-compose.ci.yml -p morghdari_ci up -d postgres redis
sleep 12
docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --no-deps \
  -e DATABASE_URL='postgres://morghdari_admin:ci_admin_password_only@postgres:5432/morghdari' \
  -e DJANGO_SETTINGS_MODULE=config.settings.test \
  migrate-gate python manage.py migrate -v 1 > /tmp/qa_mig.log 2>&1
echo "rc=$?"
grep -E "Applying|OK$" /tmp/qa_mig.log | tail -6
