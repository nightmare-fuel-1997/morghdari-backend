#!/usr/bin/env bash
# QA bring-up for poultry_vps_deploy (steps A5-A7 after data tier).
# Runs migrate as morghdari_app (house path), then bootstrap grants/seed, then full stack.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
COMPOSE="docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.vps.yml"

echo "== [1/6] role passwords"
docker exec -i morghdari-postgres psql -U "$POSTGRES_ADMIN_USER" -d "$DATABASE_NAME" -w -v ON_ERROR_STOP=1 \
  -c "ALTER ROLE morghdari_app PASSWORD '$DATABASE_PASSWORD';" \
  -c "ALTER ROLE emqx_auth PASSWORD '$EMQX_DB_PASSWORD';" \
  -c "ALTER ROLE morghdari_exporter PASSWORD '$POSTGRES_EXPORTER_PASSWORD';" || { echo FAIL-pw; exit 1; }

echo "== [2/6] migrate (admin role — DDL; app role has no CREATE on public)"
$COMPOSE run --rm --no-deps \
  -e DATABASE_URL="postgres://$POSTGRES_ADMIN_USER:$POSTGRES_ADMIN_PASSWORD@postgres:5432/$DATABASE_NAME" \
  daphne python manage.py migrate 2>&1 | tail -2 || { echo FAIL-migrate; exit 1; }

echo "== [3/6] grants"
PSQL="docker exec -i morghdari-postgres psql -U $POSTGRES_ADMIN_USER -d $DATABASE_NAME -w -v ON_ERROR_STOP=1"
$PSQL -c "GRANT USAGE ON SCHEMA public TO morghdari_app, emqx_auth, morghdari_exporter;"
$PSQL -c "GRANT SELECT ON mqtt_device_credentials, mqtt_acl_rules, gateway_slots TO emqx_auth;"
$PSQL -c "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO morghdari_app;"
$PSQL -c "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO morghdari_app;"

echo "== [4/6] re-run initdb/02_rls.sql post-migrate (idempotent per its header)"
docker exec -i morghdari-postgres psql -U "$POSTGRES_ADMIN_USER" -d "$DATABASE_NAME" -w -v ON_ERROR_STOP=1 < initdb/02_rls.sql > /tmp/rls.log 2>&1 && echo "RLS reapplied OK" || { echo FAIL-rls; tail /tmp/rls.log; exit 1; }

echo "== [5/6] subscriber credentials"
run_seed () {
  $COMPOSE run --rm --no-deps \
    -e DATABASE_URL="postgres://$POSTGRES_ADMIN_USER:$POSTGRES_ADMIN_PASSWORD@postgres:5432/$DATABASE_NAME" \
    daphne python manage.py seed_subscriber_credentials --client-id "$1" --username "$2" --password "$3" 2>&1 | tail -1
}
run_seed subscriber-01 "$SUBSCRIBER_USERNAME" "$SUBSCRIBER_PASSWORD"
run_seed subscriber-02 "$SUBSCRIBER_USERNAME_2" "$SUBSCRIBER_PASSWORD_2"

echo "== [6/6] full stack up"
$COMPOSE up -d 2>&1 | tail -2
echo "waiting for health..."
for i in $(seq 1 36); do
  n=$(docker ps --format '{{.Names}}' | grep -c morghdari- || true)
  h=$(docker inspect -f '{{.State.Health.Status}}' $(docker ps --format '{{.Names}}' | grep morghdari-) 2>/dev/null | grep -c healthy || true)
  echo "  t=${i} healthy=$h/$n"
  [ "$h" = "$n" ] && [ "$n" -ge 9 ] && break
  sleep 10
done
docker ps --format '{{.Names}} | {{.Status}}' | grep morghdari-
echo DONE
