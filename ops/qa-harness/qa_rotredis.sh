#!/usr/bin/env bash
# 17.10 helper: rotate REDIS_PASSWORD in .env and recreate dependent services.
set -euo pipefail
cd "$(dirname "$0")/.."
cp .env /tmp/qa_env_backup
NEWPW=$(openssl rand -hex 32)
sed -i "s/^REDIS_PASSWORD=.*/REDIS_PASSWORD=$NEWPW/" .env
grep "^REDIS_PASSWORD=" .env | sed 's/=.*/=<set>/'
docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.vps.yml up -d \
  redis daphne celery-worker celery-beat subscriber-01 subscriber-02
echo "recreated; waiting for health..."
for i in $(seq 1 24); do
  n=$(docker ps --format '{{.Names}}' | grep -c morghdari- || true)
  h=$(docker inspect -f '{{.State.Health.Status}}' $(docker ps --format '{{.Names}}' | grep morghdari-) 2>/dev/null | grep -c healthy || true)
  echo "  t=$i healthy=$h"
  healthy_count=$(docker inspect -f '{{.State.Health.Status}}' morghdari-redis morghdari-daphne morghdari-celery-worker morghdari-celery-beat morghdari-subscriber-01 morghdari-subscriber-02 2>/dev/null | grep -c healthy || true)
  if [ "$healthy_count" = "6" ]; then echo ALL6-HEALTHY; break; fi
  sleep 10
done
echo DONE
