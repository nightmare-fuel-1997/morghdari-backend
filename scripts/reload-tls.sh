#!/usr/bin/env bash
# Certbot renewal deploy hook (Security Architecture; Deployment Guide §7.1) —
# copies renewed files into /opt/morghdari/certs under BOTH naming conventions
# and hot-reloads nginx + EMQX without dropping connections.
# Installed at /etc/letsencrypt/renewal-hooks/deploy/reload-tls.sh by
# scripts/tls_issue_vps.sh. KEEP the copy targets in sync with the §7.1 names.

set -euo pipefail
LIVE=/etc/letsencrypt/live/api.morghdari.com
CERTS=/opt/morghdari/certs

cp "$LIVE/fullchain.pem" "$CERTS/fullchain.pem"
cp "$LIVE/privkey.pem"   "$CERTS/privkey.pem"
cp "$LIVE/chain.pem"     "$CERTS/chain.pem"
cp "$LIVE/fullchain.pem" "$CERTS/server.crt"
cp "$LIVE/privkey.pem"   "$CERTS/server.key"
cp "$LIVE/chain.pem"     "$CERTS/ca.crt"

docker exec morghdari-nginx nginx -s reload
docker exec morghdari-emqx emqx ctl listeners restart ssl:default
echo "$(date -Is) TLS renewed and hot-reloaded (nginx + emqx)" >> /var/log/morghdari/reload-tls.log
