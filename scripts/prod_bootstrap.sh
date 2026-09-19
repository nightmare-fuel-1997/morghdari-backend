#!/usr/bin/env bash
# M7-03 bootstrap — the post-migrate steps a fresh production data volume needs
# (run ONCE, after `manage.py migrate`, before `up -d` of the app services):
#
#   1. Role passwords: initdb/01_roles.sql creates emqx_auth and
#      morghdari_exporter LOGIN with NO password (it cannot know the .env) —
#      set them from the hardened .env here.
#   2. Table grants: migrations create the tables AFTER initdb ran, so the
#      default-privilege grants in 01_roles.sql never covered them (the same
#      drift the CI conftest fixed for test DBs). Grant now:
#        - emqx_auth: SELECT on the mqtt tables (broker authn/authorizer reads)
#        - morghdari_app: full DML on all tables + sequences (the request role)
#   3. Subscriber tier credentials (AU-SUBSCRIBER-N2): the N>=2 topology cannot
#      connect through the postgresql authn chain without its service accounts.

set -euo pipefail
cd "$(dirname "$0")/.."
source .env

COMPOSE="docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.rehearsal.yml"
PSQL="$COMPOSE exec -T postgres psql -U $POSTGRES_ADMIN_USER -d $DATABASE_NAME -v ON_ERROR_STOP=1"

echo "== 1. role passwords (from .env, never hardcoded)"
$PSQL -c "ALTER ROLE morghdari_app PASSWORD '$DATABASE_PASSWORD';"
$PSQL -c "ALTER ROLE emqx_auth PASSWORD '$EMQX_DB_PASSWORD';"
$PSQL -c "ALTER ROLE morghdari_exporter PASSWORD '$POSTGRES_EXPORTER_PASSWORD';"

echo "== 2. grants (tables were created by migrations, after initdb)"
$PSQL -c "GRANT USAGE ON SCHEMA public TO morghdari_app, emqx_auth, morghdari_exporter;"
$PSQL -c "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO morghdari_app;"
$PSQL -c "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO morghdari_app;"
# BUG-25 (VPS 2026-09-16/17, live on 82.115.17.252): migrations have not run
# on a FRESH volume yet, so the mqtt tables do not exist and an unconditional
# grant killed the whole bootstrap (set -e). Guard each table grant with
# to_regclass and defer what is missing to the post-migrate pass; the script
# must exit 0 both passes.
$PSQL <<'SQL'
DO $$
BEGIN
  IF to_regclass('public.mqtt_device_credentials') IS NOT NULL THEN
    GRANT SELECT ON mqtt_device_credentials TO emqx_auth;
  ELSE
    RAISE NOTICE 'deferred: mqtt_device_credentials does not exist yet (pre-migrate pass)';
  END IF;
  IF to_regclass('public.mqtt_acl_rules') IS NOT NULL THEN
    GRANT SELECT ON mqtt_acl_rules TO emqx_auth;
  ELSE
    RAISE NOTICE 'deferred: mqtt_acl_rules does not exist yet (pre-migrate pass)';
  END IF;
  IF to_regclass('public.gateway_slots') IS NOT NULL THEN
    GRANT SELECT ON gateway_slots TO emqx_auth;
  ELSE
    RAISE NOTICE 'deferred: gateway_slots does not exist yet (pre-migrate pass)';
  END IF;
END
$$;
SQL

echo "== 3. subscriber tier credentials (shared-subscription group members)"
# Runs as morghdari_admin: seeding PLATFORM infrastructure (org + slots)
# through FORCE-RLS as the app role is correctly fail-closed — the admin role
# (BYPASSRLS) is the sanctioned bootstrap path, same as migrate.
run_seed () {  # client-id username password
  $COMPOSE run --rm --no-deps \
    -e DATABASE_URL="postgres://$POSTGRES_ADMIN_USER:$POSTGRES_ADMIN_PASSWORD@postgres:5432/$DATABASE_NAME" \
    daphne python manage.py \
    seed_subscriber_credentials --client-id "$1" --username "$2" --password "$3"
}
# BUG-25: the seeds need migrated tables (organizations, gateway_slots, ...).
# On a fresh pre-migrate volume they cannot run — defer them, exit 0.
# NOTE: on the VPS (82.115.17.252) the compose override is
# -f docker-compose.vps.yml instead of the rehearsal file above; re-run this
# script AFTER manage.py migrate in both environments.
SEEDS_READY=$($PSQL -tAc "SELECT to_regclass('public.gateway_slots') IS NOT NULL")
if [ "$SEEDS_READY" = "t" ]; then
  run_seed subscriber-01 "$SUBSCRIBER_USERNAME" "$SUBSCRIBER_PASSWORD"
  run_seed subscriber-02 "$SUBSCRIBER_USERNAME_2" "$SUBSCRIBER_PASSWORD_2"
else
  echo "deferred: subscriber seeds need migrated tables — re-run prod_bootstrap.sh AFTER manage.py migrate"
fi

echo "== bootstrap complete"
