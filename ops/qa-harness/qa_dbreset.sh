#!/usr/bin/env bash
set -uo pipefail
cd /m/projects/poultry_vps_deploy
PS() { docker exec -i morghdari-postgres psql -U morghdari_admin -d postgres -w "$@"; }
echo "== activity:"
PS -t -A -c "SELECT pid, datname, state, wait_event_type, left(regexp_replace(query,'\n',' ','g'),60) FROM pg_stat_activity WHERE backend_type='client backend';"
echo "== kill stragglers on morghdari/postgres except ours"
PS -t -A -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('morghdari','postgres') AND pid <> pg_backend_pid() AND backend_type='client backend';" | wc -l
sleep 2
echo "== drop invalid db"
PS -c "DROP DATABASE IF EXISTS morghdari;" 
echo "drop rc=$?"
echo "== create"
PS -c "CREATE DATABASE morghdari OWNER morghdari_admin;"
echo "create rc=$?"
PS -c "GRANT ALL ON SCHEMA public TO morghdari_owner, morghdari_app, morghdari_admin;"
echo "== verify"
PS -t -A -c "SELECT datname FROM pg_database WHERE datname LIKE 'morghdari%';"
echo DONE
