#!/usr/bin/env bash
# =============================================================================
# run_acceptance.sh — the ONE-command acceptance ritual (v0.6 exit gate).
#
# Runs, in order, each gate REQUIRED to pass before any release or deploy:
#   1. migrate-gate   — migration deployability (makemigrations --check +
#                       real forward migrate + idempotency + reversibility
#                       posture, on a throwaway DB it creates and drops).
#   2. test           — the full pytest suite with the 80% coverage gate.
#   3. image-scan     — trivy CRITICAL/HIGH (fixable) CVEs on the built image.
#
# WHY the flags are NON-NEGOTIABLE here (and hardcoded, not arguments):
#   -p morghdari_ci : CONSTRAINTS C-13. Without a project name, compose derives
#                     one from the directory and its postgres/redis REPLACE the
#                     dev stack's containers (bit AGAIN on 2026-09-06 — the dev
#                     :5432 postgres came up with CI credentials; data survived
#                     only because the named volume did).
#   --build         : the stale-image trap. Images are cached; without --build
#                     the gates validate OLD source (it quietly reported a
#                     pre-M6 era count as "green"). A green ritual MUST mean
#                     green NOW.
#
# Rollback posture for the deployment this gates: see DEPLOYMENT.md.
#
# فارسی: آیینِ پذیرشِ یک‌فرمانی؛ هر سه گیت باید سبز شوند — و همیشه با
# -p morghdari_ci و --build، وگرنه یا دیتابیسِ dev عوض می‌شود یا تصویر کهنه
# «سبز»ِ دروغین می‌دهد.
# =============================================================================

set -euo pipefail

PROJ="morghdari_ci"
COMPOSE=(docker compose -f docker-compose.ci.yml -p "$PROJ")

banner() { printf '\n=== %s ===\n' "$1"; }

# CI containers from a PREVIOUS run would collide with -p morghdari_ci names.
if docker ps --format '{{.Names}}' | grep -q "^${PROJ}-"; then
  banner "tearing down stale ${PROJ} containers"
  "${COMPOSE[@]}" down --remove-orphans >/dev/null
fi

banner "GATE 1/3 — migrate-gate (deployability)"
"${COMPOSE[@]}" run --rm --build migrate-gate

banner "GATE 2/3 — test (pytest + coverage)"
"${COMPOSE[@]}" run --rm --build test

banner "GATE 3/3 — image-scan (trivy CRITICAL/HIGH, fixable)"
"${COMPOSE[@]}" run --rm image-scan

banner "teardown"
"${COMPOSE[@]}" down --remove-orphans >/dev/null

banner "ACCEPTANCE: ALL GATES GREEN"
echo "next: tag/deploy only from this exact tree state (see DEPLOYMENT.md)"
