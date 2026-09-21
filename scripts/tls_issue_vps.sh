#!/usr/bin/env bash
# M7-02 (VPS day) - certbot webroot issuance + the A7.1 copy step.
# Run ON the VPS before the first `docker compose up` (nginx + EMQX refuse to
# boot without certs). DNS A records for every requested hostname must already
# point at the VPS.
#
# Where the settings come from (first one wins):
#   1. environment variables   (CERTIFICATE_EMAIL=... sudo -E / sudo VAR=value ...)
#   2. the repo's .env file    (recommended - the same file the stack reads)
#   3. built-in defaults       (api.morghdari.com / mqtt.morghdari.com)
#
# Usage:
#   sudo bash scripts/tls_issue_vps.sh              # normal run (reads .env)
#   DRY_RUN=1 bash scripts/tls_issue_vps.sh         # preflight only, no changes
#   sudo CERTIFICATE_EMAIL=you@example.com bash scripts/tls_issue_vps.sh
#
# 2026-09-16 fixes (VPS day-1, host 185.8.175.150):
#   1. THE BUG: the script only read CERTIFICATE_EMAIL from the *environment*,
#      but `sudo` runs with `Defaults env_reset` (stock Ubuntu sudoers), so
#      every `export CERTIFICATE_EMAIL=... ; sudo bash scripts/...` from
#      README_VPS.md died with:
#        scripts/tls_issue_vps.sh: line 14: CERTIFICATE_EMAIL: export CERTIFICATE_EMAIL first
#      The script now sources the repo .env (like prod_bootstrap.sh and
#      walg_base_backup.sh already do), so the value lives in one place and
#      survives sudo.
#   2. Location-independent: the old `cd /opt/morghdari` broke any clone at
#      another path. The repo root is derived from this script's own path.
#   3. `certs/` is git-ignored, so a fresh clone lacks it and the post-issuance
#      `cp ... certs/...` failed. It is created now.
#   4. MQTT_DOMAIN may be empty -> single-name certificate (useful before the
#      mqtt.<domain> A record exists); otherwise both names are requested.
#   5. The temporary port-80 webroot container is named and removed on exit
#      (the old `kill $PID` could leave it holding port 80, which then breaks
#      the real nginx start).
#   6. The renewal hook is installed with THIS deployment's LIVE + CERTS paths
#      baked in.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- capture explicit environment values BEFORE .env so they keep priority ---
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
WEBROOT="$REPO_ROOT/certbot-www"
LIVE_DIR="/etc/letsencrypt/live/$PRIMARY_DOMAIN"
ACME_CONTAINER="morghdari-acme-webroot"
DNS_FAIL=0

if [ -n "$MQTT_DOMAIN" ]; then
  DOMAIN_ARGS=(-d "$PRIMARY_DOMAIN" -d "$MQTT_DOMAIN")
  DNS_LIST="$PRIMARY_DOMAIN $MQTT_DOMAIN"
else
  DOMAIN_ARGS=(-d "$PRIMARY_DOMAIN")
  DNS_LIST="$PRIMARY_DOMAIN"
fi

echo "== preflight"
echo "   repo root  : $REPO_ROOT"
echo "   .env loaded: $ENV_FILE_LOADED"
echo "   API domain : $PRIMARY_DOMAIN"
if [ -n "$MQTT_DOMAIN" ]; then
  echo "   MQTT domain: $MQTT_DOMAIN"
else
  echo "   MQTT domain: (none - issuing a single-name certificate)"
fi
echo "   contact    : ${EMAIL:-<missing>}"

[ -f docker-compose.prod.yml ] || {
  echo "FATAL: docker-compose.prod.yml not found under $REPO_ROOT - run this from the cloned repo." >&2
  exit 1
}
[ -n "$EMAIL" ] || {
  echo "FATAL: no contact address. Set CERTIFICATE_EMAIL in .env (recommended)" >&2
  echo "       or pass it explicitly: sudo CERTIFICATE_EMAIL=you@example.com bash scripts/tls_issue_vps.sh" >&2
  exit 1
}
case "$EMAIL" in
  *@*.*) : ;;
  *)
    echo "FATAL: CERTIFICATE_EMAIL='$EMAIL' is not an email address (expected user@domain)." >&2
    echo "       Let's Encrypt rejects a bare hostname; use the address that should receive expiry warnings." >&2
    exit 1
    ;;
esac
command -v certbot >/dev/null 2>&1 || {
  echo "FATAL: certbot is not installed.  sudo apt update && sudo apt install -y certbot" >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || {
  echo "FATAL: docker is not installed, or not on PATH for this shell." >&2
  exit 1
}

if [ "${SKIP_DNS_CHECK:-0}" != "1" ]; then
  for d in $DNS_LIST; do
    if getent hosts "$d" >/dev/null 2>&1; then
      echo "   DNS ok     : $d"
    else
      echo "   DNS missing: $d does not resolve - the ACME challenge would fail." >&2
      DNS_FAIL=1
    fi
  done
  if [ "$DNS_FAIL" = "1" ]; then
    echo "FATAL: point the A record of every requested name at this VPS first." >&2
    echo "       (SKIP_DNS_CHECK=1 overrides; unset MQTT_DOMAIN for a single-name cert.)" >&2
    exit 1
  fi
else
  echo "   DNS check  : skipped (SKIP_DNS_CHECK=1)"
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "== DRY_RUN=1: preflight only - nothing issued, nothing copied, nothing installed."
  exit 0
fi

cleanup() {
  sudo docker rm -f "$ACME_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$WEBROOT" certs

sudo docker rm -f "$ACME_CONTAINER" >/dev/null 2>&1 || true
sudo docker run --rm --name "$ACME_CONTAINER" -p 80:80 \
  -v "$WEBROOT":/usr/share/nginx/html:ro nginx:1.25-alpine &

# Wait for the temporary webroot server to actually answer before asking the CA.
# A fixed `sleep 3` raced the container start and produced a transient
# "Timeout during connect" ACME failure on 2026-09-16.
READY=0
for _ in $(seq 1 20); do
  # ANY HTTP response proves the server is up (the stock nginx answers 403 for
  # the directory index, so a 2xx-only test would never pass).
  code="$(curl -s -m 2 -o /dev/null -w '%{http_code}' http://127.0.0.1/ 2>/dev/null || echo 000)"
  if [ "$code" != "000" ]; then READY=1; break; fi
  sleep 1
done
if [ "$READY" != "1" ]; then
  echo "FATAL: the temporary webroot container did not answer on port 80." >&2
  exit 1
fi
echo "   webroot    : answering on port 80"

sudo certbot certonly --webroot -w "$WEBROOT" \
  "${DOMAIN_ARGS[@]}" --email "$EMAIL" --agree-tos --non-interactive

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

# The renewal hook runs from /etc/letsencrypt/renewal-hooks/deploy and cannot
# infer the repo path, so its LIVE + CERTS lines are baked in at install time.
sed -e "s|^LIVE=.*|LIVE=$LIVE_DIR|" \
    -e "s|^CERTS=.*|CERTS=$REPO_ROOT/certs|" \
    scripts/reload-tls.sh \
  | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-tls.sh >/dev/null
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-tls.sh

echo "TLS issued for: $DNS_LIST"
echo "Staged under both naming conventions in $REPO_ROOT/certs."
echo "Deploy hook installed with LIVE=$LIVE_DIR and CERTS=$REPO_ROOT/certs."
echo "Rehearse ONE renewal now (a renewal that fails silently is an outage in 60 days):"
echo "  sudo certbot renew --dry-run"
