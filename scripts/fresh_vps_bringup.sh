#!/usr/bin/env bash
# =============================================================================
# fresh_vps_bringup.sh — one-shot bring-up of the morghdari stack on a FRESH
# Ubuntu 22.04/24.04 VPS (proven on 82.115.17.252, Ubuntu 24.04.5, 2026-09-17).
#
# Usage (as root, from the repo root on the VPS with .env already in place):
#   bash scripts/fresh_vps_bringup.sh
#
# Encodes the full proven sequence from the 2026-09-17 deployment ledger:
#   deps -> data services -> bootstrap pass 1 (BUG-25 deferred grants)
#        -> migrate as ADMIN (BUG-1) -> bootstrap pass 2 (grants + seeds)
#        -> full stack -> in-band health check
# Idempotent: safe to re-run; each stage skips or no-ops when already done.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

echo "== [1/8] docker + compose"
if ! command -v docker >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq docker.io docker-compose-plugin openssl curl git
  systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin missing"; exit 1; }

echo "== [2/8] cert material sanity (BUG-24 perms)"
[ -f certs/fullchain.pem ] && [ -f certs/privkey.pem ] || { echo "certs/ not populated - run scripts/tls_stage_rehearsal.sh or tls_issue_vps.sh first"; exit 1; }
chgrp 1000 certs/privkey.pem certs/server.key 2>/dev/null || true
chmod 0640 certs/privkey.pem certs/server.key
chmod 0644 certs/ca.crt certs/chain.pem certs/fullchain.pem certs/server.crt
[ "$(stat -c %a .env)" = "600" ] || chmod 600 .env
if grep -qE '=(change-me|change\.\.\.-min)' .env; then echo "FATAL: .env still has change-me placeholders (BUG-28) - fill every value before first boot"; exit 1; fi
ALLOWED=$(grep '^DJANGO_ALLOWED_HOSTS=' .env | cut -d= -f2)
case ",$ALLOWED," in *,82.115.17.252,*) ;; *) sed -i 's/^DJANGO_ALLOWED_HOSTS=.*/&,82.115.17.252/' .env; echo "added 82.115.17.252 to DJANGO_ALLOWED_HOSTS";; esac

export COMPOSE="docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.vps.yml"

echo "== [3/8] build images (local, no registry)"
$COMPOSE build --pull postgres daphne || { echo "build failed (set PIP_INDEX_URL in .env if pypi unreachable - BUG-26)"; exit 1; }

echo "== [4/8] data services first"
$COMPOSE up -d --no-deps postgres redis
sleep 25
docker ps --filter name=morghdari-postgres --filter name=morghdari-redis --format '{{.Names}} {{.Status}}'

echo "== [5/8] bootstrap pass 1 (passwords; BUG-25 deferred grants OK pre-migrate)"
bash scripts/prod_bootstrap.sh || exit 1

echo "== [6/8] migrate as ADMIN (BUG-1: app role lacks CREATE on public, PG15+)"
source .env
$COMPOSE run --rm -e DATABASE_URL="postgres://${POSTGRES_ADMIN_USER}:${POSTGRES_ADMIN_PASSWORD}@postgres:5432/${DATABASE_NAME}" daphne python manage.py migrate --noinput || exit 1

echo "== [7/8] bootstrap pass 2 (mqtt grants + subscriber seeds) + full stack"
bash scripts/prod_bootstrap.sh || exit 1
$COMPOSE up -d || exit 1

echo "== [8/8] in-band smoke"
sleep 30
docker ps --format '{{.Names}} {{.Status}}'
echo -n 'health: '
curl -sk --max-time 15 https://127.0.0.1/api/v1/health/ | head -c 120; echo
echo "== bring-up complete - next: bash scripts/vps_smoke.sh, then external curl checks"
