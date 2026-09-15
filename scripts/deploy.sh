#!/usr/bin/env bash
# deploy.sh — one-command deploy AND rollback (M7-09, Mentor Plan 1.4: "rollback
# you haven't drilled is fiction").
#
#   ./scripts/deploy.sh <tag>          # deploy ghcr.io/morghdari/backend:<tag>
#   ./scripts/deploy.sh <prev-tag>     # rollback = the same one command
#
# Mechanics: the prod compose pins image ghcr.io/morghdari/backend:${MORGHDARI_TAG};
# a shell env var overrides the .env file, so this script never mutates .env
# (secrets stay put). After `up -d` it gates on container health; if the gate
# fails it redeploys the previous tag AUTOMATICALLY and exits non-zero — at 3am
# the operator runs one command and gets either a healthy stack or the old one.
#
# Laptop rehearsal (v0.7): the rehearsal override adds build contexts when
# present; on the VPS it simply doesn't exist and the script skips it.

set -uo pipefail

TAG="${1:-}"
if [[ -z "$TAG" ]]; then
  echo "usage: deploy.sh <tag>   (deploy = same command as rollback)" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
COMPOSE=(-p morghdari --env-file .env -f docker-compose.prod.yml)
[[ -f docker-compose.rehearsal.yml ]] && COMPOSE+=(-f docker-compose.rehearsal.yml)
APP_SERVICES=(daphne celery-worker celery-beat subscriber-01 subscriber-02)
ALL_CONTAINERS=(morghdari-nginx morghdari-emqx morghdari-redis morghdari-postgres
                morghdari-daphne morghdari-celery-worker morghdari-celery-beat
                morghdari-subscriber-01 morghdari-subscriber-02)

STATE_FILE=".deploy_tag"
PREV=""
[[ -f "$STATE_FILE" ]] && PREV="$(cat "$STATE_FILE")"

 unhealthy() {
  local bad=()
  for c in "${ALL_CONTAINERS[@]}"; do
    s="$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)"
    [[ "$s" != "healthy" ]] && bad+=("$c:$s")
  done
  [[ ${#bad[@]} -eq 0 ]] && return 1
  echo "unhealthy: ${bad[*]}" >&2
  return 0
}

gate() {  # wait up to 120s for every container to report healthy
  for _ in $(seq 1 24); do
    sleep 5
    if ! unhealthy; then return 0; fi
  done
  return 1
}

echo "==> deploying tag: $TAG (previous: ${PREV:-<none recorded>})"
if ! MORGHDARI_TAG="$TAG" docker compose "${COMPOSE[@]}" up -d "${APP_SERVICES[@]}"; then
  echo "!! compose up failed for tag $TAG" >&2
  if [[ -n "$PREV" ]]; then
    echo "==> rolling back to $PREV" >&2
    MORGHDARI_TAG="$PREV" docker compose "${COMPOSE[@]}" up -d "${APP_SERVICES[@]}"
  fi
  exit 1
fi

if gate; then
  # BUG-14: app containers were just recreated with new IPs; nginx holds a
  # STATIC upstream (`server daphne:8000`) that the `resolver` line does NOT
  # re-resolve, so without this reload every API/WS request 502s while the
  # container health gate stays green. Best-effort, but reported.
  if docker exec morghdari-nginx nginx -s reload 2>/dev/null; then
    echo "==> nginx reloaded (fresh upstream IPs)"
  else
    echo "!! nginx reload failed - API/WS may 502 until a manual reload" >&2
  fi
  echo "$TAG" > "$STATE_FILE"
  echo "==> OK: all 9 containers healthy on tag $TAG"
  exit 0
fi

echo "!! health gate FAILED on tag $TAG" >&2
if [[ -z "$PREV" ]]; then
  echo "!! no previous tag recorded — NOT rolling back (operator decision)" >&2
  exit 1
fi
echo "==> AUTOMATIC ROLLBACK to $PREV" >&2
if MORGHDARI_TAG="$PREV" docker compose "${COMPOSE[@]}" up -d "${APP_SERVICES[@]}" && gate; then
  echo "==> ROLLED BACK: stack healthy again on $PREV"
  exit 1   # deploy failed even though rollback succeeded — loud, always
fi
echo "!! ROLLBACK ALSO UNHEALTHY — page the human" >&2
exit 2
