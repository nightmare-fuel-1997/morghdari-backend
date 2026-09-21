#!/usr/bin/env bash
# M7-02 fallback (2026-09-16) - issue the certificate with a DNS-01 challenge
# when inbound TCP/80 is not reachable from Let's Encrypt's validators.
#
# Use this when `tls_issue_vps.sh` fails with:
#   "Timeout during connect (likely firewall problem)"
# DNS-01 proves domain control with a TXT record instead of an inbound HTTP
# request, so it works behind a provider firewall that blocks 80/443.
#
# Settings come from the same .env as tls_issue_vps.sh
# (CERTIFICATE_EMAIL, PRIMARY_DOMAIN, MQTT_DOMAIN).
#
# Usage:
#   DRY_RUN=1 bash scripts/tls_issue_vps_dns.sh        # preflight only
#   sudo bash scripts/tls_issue_vps_dns.sh             # manual: add a TXT record
#   sudo DNS_PLUGIN=cloudflare bash scripts/tls_issue_vps_dns.sh   # automated
#
# Automation note: --manual means the TXT record must be re-created at every
# renewal (~every 60 days) by hand. Set DNS_PLUGIN to a certbot DNS plugin
# (cloudflare, route53, google, ...) with its credentials file in place to make
# renewals unattended. The long-term fix for this host is still to open 80/443
# at the provider and use tls_issue_vps.sh.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

_CLI_EMAIL="${CERTIFICATE_EMAIL-}"
_CLI_PRIMARY="${PRIMARY_DOMAIN-}"
_CLI_MQTT="${MQTT_DOMAIN-__UNSET__}"

ENV_FILE_LOADED="no"
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
  ENV_FILE_LOADED="yes"
fi

EMAIL="${_CLI_EMAIL:-${CERTIFICATE_EMAIL:-}}"
PRIMARY_DOMAIN="${_CLI_PRIMARY:-${PRIMARY_DOMAIN:-api.morghdari.com}}"
if [ "$_CLI_MQTT" != "__UNSET__" ]; then
  MQTT_DOMAIN="$_CLI_MQTT"
else
  MQTT_DOMAIN="${MQTT_DOMAIN-mqtt.morghdari.com}"
fi

DRY_RUN="${DRY_RUN:-0}"
DNS_PLUGIN="${DNS_PLUGIN:-}"
LIVE_DIR="/etc/letsencrypt/live/$PRIMARY_DOMAIN"

if [ -n "$MQTT_DOMAIN" ]; then
  DOMAIN_ARGS=(-d "$PRIMARY_DOMAIN" -d "$MQTT_DOMAIN")
  NAME_LIST="$PRIMARY_DOMAIN + $MQTT_DOMAIN"
else
  DOMAIN_ARGS=(-d "$PRIMARY_DOMAIN")
  NAME_LIST="$PRIMARY_DOMAIN"
fi

echo "== preflight (DNS-01 path)"
echo "   repo root   : $REPO_ROOT"
echo "   .env loaded : $ENV_FILE_LOADED"
echo "   names       : $NAME_LIST"
echo "   contact     : ${EMAIL:-<missing>}"
echo "   challenge   : DNS-01 (no inbound port required)"
if [ -n "$DNS_PLUGIN" ]; then
  echo "   mode        : automated via certbot --dns-$DNS_PLUGIN"
else
  echo "   mode        : manual (certbot will ask you to create a TXT record)"
fi

[ -f docker-compose.prod.yml ] || {
  echo "FATAL: docker-compose.prod.yml not found under $REPO_ROOT - run this from the cloned repo." >&2
  exit 1
}
[ -n "$EMAIL" ] || {
  echo "FATAL: no contact address. Set CERTIFICATE_EMAIL in .env." >&2
  exit 1
}
case "$EMAIL" in
  *@*.*) : ;;
  *) echo "FATAL: CERTIFICATE_EMAIL='$EMAIL' is not an email address." >&2; exit 1 ;;
esac
command -v certbot >/dev/null 2>&1 || {
  echo "FATAL: certbot is not installed.  sudo apt update && sudo apt install -y certbot" >&2
  exit 1
}
if [ -n "$DNS_PLUGIN" ]; then
  python3 - "$DNS_PLUGIN" <<'PY' || true
import importlib.util, sys
mod = "certbot_dns_" + sys.argv[1]
print(f"   plugin check: {mod} {'present' if importlib.util.find_spec(mod) else 'MISSING - install it first'}")
PY
fi

for d in "$PRIMARY_DOMAIN" "$MQTT_DOMAIN"; do
  [ -n "$d" ] || continue
  if getent hosts "$d" >/dev/null 2>&1; then
    echo "   resolves    : $d"
  else
    echo "   no A record : $d (fine for DNS-01 - the TXT record is what matters)"
  fi
done

if [ "$DRY_RUN" = "1" ]; then
  echo "== DRY_RUN=1: preflight only - nothing issued, nothing copied."
  exit 0
fi

cleanup_noop() { :; }
trap cleanup_noop EXIT

mkdir -p certs

if [ -n "$DNS_PLUGIN" ]; then
  sudo certbot certonly --non-interactive --agree-tos --email "$EMAIL" \
    "--dns-$DNS_PLUGIN" "${DOMAIN_ARGS[@]}"
else
  cat <<'EOM'

------------------------------------------------------------------
MANUAL CHALLENGE - have your DNS panel open.
certbot will print a TXT record like:

    _acme-challenge.<your-domain>   TXT   "<long value>"

Create it at your DNS provider, wait for propagation
(e.g. `dig TXT _acme-challenge.<your-domain>` from another host),
then come back here and press Enter.
------------------------------------------------------------------

EOM
  sudo certbot certonly --manual --preferred-challenges dns \
    "${DOMAIN_ARGS[@]}" --agree-tos --email "$EMAIL"
fi

sudo cp "$LIVE_DIR/fullchain.pem" certs/fullchain.pem
sudo cp "$LIVE_DIR/privkey.pem"   certs/privkey.pem
sudo cp "$LIVE_DIR/chain.pem"     certs/chain.pem
sudo cp "$LIVE_DIR/fullchain.pem" certs/server.crt
sudo cp "$LIVE_DIR/privkey.pem"   certs/server.key
sudo cp "$LIVE_DIR/chain.pem"     certs/ca.crt

# The EMQX container runs as uid 1000 and must read the private key. certbot
# writes it 0600 root:root, which makes the broker's TLS listener fail EVERY
# handshake while still reporting "running" (observed 2026-09-16 on
# 185.8.175.150: ssl_error 269, subscribers logging "Connection reset by
# peer", "permission denied" reading server.key). nginx runs as root in its
# container and keeps read access with 0640 root:1000.
sudo chgrp 1000 certs/privkey.pem certs/server.key
sudo chmod 0640 certs/privkey.pem certs/server.key

sed -e "s|^LIVE=.*|LIVE=$LIVE_DIR|" \
    -e "s|^CERTS=.*|CERTS=$REPO_ROOT/certs|" \
    scripts/reload-tls.sh \
  | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-tls.sh >/dev/null
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-tls.sh

echo "TLS issued for: $NAME_LIST"
echo "Staged under both naming conventions in $REPO_ROOT/certs."
if [ -z "$DNS_PLUGIN" ]; then
  echo "REMINDER: this certificate was issued manually. Renewal (~60 days) will"
  echo "          need the TXT record again. Set DNS_PLUGIN, or open port 80 at"
  echo "          the provider and switch back to scripts/tls_issue_vps.sh."
fi
