#!/usr/bin/env bash
# M7-02 rehearsal — stage TLS material under BOTH naming conventions
# (Deployment Guide §7.1) WITHOUT Let's Encrypt (no public DNS on a laptop).
#
# VPS day: use scripts/tls_issue_vps.sh (certbot webroot) instead — same copy
# step, real certs. This script exists so nginx + EMQX boot and the TLS
# plumbing (nginx 443, EMQX 8883, deploy-hook file names) is rehearsed
# end-to-end on the staging laptop.
#
# A SAN self-signed cert covering api.morghdari.com / mqtt.morghdari.com /
# localhost; chain.pem = the cert itself (self-signed has no chain — OCSP
# stapling degrades to a warning, which is acceptable on staging only).

set -euo pipefail
cd "$(dirname "$0")/.."

DAYS=825
mkdir -p certs
# MSYS_NO_PATHCONV: Git Bash must not rewrite the /CN= subject into a Windows path.
MSYS_NO_PATHCONV=1 openssl req -x509 -nodes -newkey rsa:2048 -days "$DAYS" \
  -keyout certs/privkey.pem -out certs/fullchain.pem \
  -subj "/CN=api.morghdari.com" \
  -addext "subjectAltName=DNS:api.morghdari.com,DNS:mqtt.morghdari.com,DNS:localhost"

# BOTH naming conventions (§7.1): nginx uses fullchain/privkey/chain,
# EMQX uses server.crt/server.key/ca.crt.
cp certs/fullchain.pem certs/chain.pem
cp certs/fullchain.pem certs/server.crt
cp certs/privkey.pem  certs/server.key
cp certs/fullchain.pem certs/ca.crt

echo "staged certs:" && ls -la certs/
