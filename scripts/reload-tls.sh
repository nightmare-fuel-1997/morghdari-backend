#!/usr/bin/env bash
# Certbot renewal deploy hook (Security Architecture; Deployment Guide A7.1) -
# copies renewed files into the deployment's certs/ dir under BOTH naming
# conventions and hot-reloads nginx + EMQX without dropping connections.
# Installed at /etc/letsencrypt/renewal-hooks/deploy/reload-tls.sh by
# scripts/tls_issue_vps.sh, which bakes LIVE + CERTS for the deployment.
# KEEP the copy targets in sync with the A7.1 names.
#
# Manual run (the defaults are the documented /opt/morghdari layout):
#   LIVE=/etc/letsencrypt/live/<domain> CERTS=<repo>/certs bash scripts/reload-tls.sh
#
# 2026-09-16: LIVE and CERTS are overridable instead of hardcoded, and the
# target/log directories are created when missing - a renewal that fails
# silently is an outage in 60 days, so the hook must not die on a missing dir.

set -euo pipefail
LIVE="${LIVE:-/etc/letsencrypt/live/api.morghdari.com}"
CERTS="${CERTS:-/opt/morghdari/certs}"
LOG="${LOG:-/var/log/morghdari/reload-tls.log}"

mkdir -p "$CERTS" "$(dirname "$LOG")"

cp "$LIVE/fullchain.pem" "$CERTS/fullchain.pem"
cp "$LIVE/privkey.pem"   "$CERTS/privkey.pem"
cp "$LIVE/chain.pem"     "$CERTS/chain.pem"
cp "$LIVE/fullchain.pem" "$CERTS/server.crt"
cp "$LIVE/privkey.pem"   "$CERTS/server.key"
cp "$LIVE/chain.pem"     "$CERTS/ca.crt"

# Keep the private key readable by the EMQX container user (uid 1000): the copy
# above comes from certbot at 0600 root, and that silently breaks MQTT on the
# next renewal (the listener still reports "running"). See tls_issue_vps.sh.
chgrp 1000 "$CERTS/privkey.pem" "$CERTS/server.key" 2>/dev/null || true
chmod 0640 "$CERTS/privkey.pem" "$CERTS/server.key" 2>/dev/null || true

# 2026-09-17 (live issuance on 82.115.17.252): ca.crt must let Python TLS
# clients (subscribers validate with ca_certs=ca.crt) build a FULL chain.
# Let's Encrypt chain.pem holds only intermediates; append the ISRG Root X1
# trust anchor from the host CA store when present (idempotent).
if [ -f /etc/ssl/certs/ISRG_Root_X1.pem ] && ! grep -q "ISRG Root X1" "$CERTS/ca.crt" 2>/dev/null; then
  cat /etc/ssl/certs/ISRG_Root_X1.pem >> "$CERTS/ca.crt"
fi

# Best-effort reloads (same philosophy as scripts/deploy.sh): the certificates
# are already staged, so a container that happens to be down must not fail the
# renewal hook and send certbot into a retry loop.
docker exec morghdari-nginx nginx -s reload \
  || echo "WARN: nginx reload failed (container down?); certificates are staged." >&2
docker exec morghdari-emqx emqx ctl listeners restart ssl:default \
  || echo "WARN: EMQX listener restart failed (container down?); certificates are staged." >&2

echo "$(date -Is) TLS renewed and hot-reloaded (nginx + emqx)" >> "$LOG"
