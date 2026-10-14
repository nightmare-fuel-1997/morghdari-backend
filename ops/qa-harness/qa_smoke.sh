#!/usr/bin/env bash
# Run vps_smoke.sh with a python3 shim (Git Bash python3 is a WindowsApps stub).
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
mkdir -p /tmp/qabin
printf '#!/bin/sh\nexec "C:/Users/mrboz/AppData/Local/hermes/hermes-agent/venv/Scripts/python" "$@"\n' > /tmp/qabin/python3
chmod +x /tmp/qabin/python3
PATH="/tmp/qabin:$PATH" SMOKE_USER=demo-owner SMOKE_PASSWORD=password12345 bash scripts/vps_smoke.sh
