#!/usr/bin/env bash
# M7-02 (VPS day) — certbot webroot issuance + the §7.1 copy step.
# Run ON the VPS before the first `docker compose up` (nginx + EMQX refuse to
# boot without certs). DNS A records for api.morghdari.com AND mqtt.morghdari.com
# must already point at the VPS.
#
# Port 80 serves the ACME webroot while certbot runs; nginx itself cannot start
# yet (it needs the very certificate being issued), so a temporary static
# server holds the webroot.

set -euo pipefail
cd /opt/morghdari

EMAIL="${CERTIFICATE_EMAIL:?export CERTIFICATE_EMAIL first}"
mkdir -p certbot-www

sudo docker run --rm -p 80:80 -v /opt/morghdari/certbot-www:/usr/share/nginx/html:ro nginx:1.25-alpine &
WEBROOT_PID=$!
sleep 3

sudo certbot certonly --webroot -w /opt/morghdari/certbot-www \
  -d api.morghdari.com -d mqtt.morghdari.com --email "$EMAIL" --agree-tos --non-interactive

sudo cp /etc/letsencrypt/live/api.morghdari.com/fullchain.pem certs/fullchain.pem
sudo cp /etc/letsencrypt/live/api.morghdari.com/privkey.pem  certs/privkey.pem
sudo cp /etc/letsencrypt/live/api.morghdari.com/chain.pem    certs/chain.pem
sudo cp /etc/letsencrypt/live/api.morghdari.com/fullchain.pem certs/server.crt
sudo cp /etc/letsencrypt/live/api.morghdari.com/privkey.pem  certs/server.key
sudo cp /etc/letsencrypt/live/api.morghdari.com/chain.pem    certs/ca.crt

sudo install -m 0755 scripts/reload-tls.sh /etc/letsencrypt/renewal-hooks/deploy/reload-tls.sh

sudo kill "$WEBROOT_PID" 2>/dev/null || true
echo "TLS issued and staged under both naming conventions; deploy hook installed."
echo "Rehearse ONE renewal now (a renewal that fails silently is an outage in 60 days):"
echo "  sudo certbot renew --dry-run"
