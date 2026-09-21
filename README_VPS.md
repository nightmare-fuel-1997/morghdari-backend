# Morghdari Backend — VPS Bring-Up & API Test Guide

> Source snapshot: commit `2d6a3f9` (`v0.9-pilot-backend` + DR wave M8-01/03/04-H).
> Target: Ubuntu 22.04/24.04 VPS, 2 cores / 4 GB RAM, Docker Engine + Compose v2.

Every command below runs **on the VPS** from the repo root (`/opt/morghdari`
recommended). Nothing here needs the laptop rehearsal files — they are
deliberately not in this repo.

## 0. Prerequisites

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin openssl curl
sudo usermod -aG docker $USER   # re-login after this
```

Open firewall ports: `80`, `443` (API), `8883` (gateways/MQTT).

## 1. Clone & configure

```bash
git clone <your-github-url> /opt/morghdari && cd /opt/morghdari
cp .env.vps.example .env
nano .env   # fill EVERY value; generate secrets: openssl rand -hex 32
```

`.env` never goes to git — it is ignored by design.

## 2. TLS (pick ONE path)

**Path A — real domain (production-like).** DNS `A` records for your API and
MQTT hostnames must already point at the VPS:

```bash
# 1. put the settings in .env - sudo resets the environment, so an exported
#    shell variable never reaches the script (2026-09-16 VPS-day-1 finding):
#      CERTIFICATE_EMAIL=you@example.com     <- a mailbox, NOT a hostname
#      PRIMARY_DOMAIN=api.<your-domain>
#      MQTT_DOMAIN=mqtt.<your-domain>        <- empty = single-name certificate
# 2. rehearse the preflight, then issue:
DRY_RUN=1 bash scripts/tls_issue_vps.sh
sudo bash scripts/tls_issue_vps.sh
```

If issuance fails with `Timeout during connect (likely firewall problem)`,
TCP/80 is filtered upstream of the host (provider firewall): open 80 + 443, or
use the DNS-01 path, which needs no inbound port:

```bash
sudo bash scripts/tls_issue_vps_dns.sh
```

**Path B — IP-only API test (no domain).** Self-signed staging certs; nginx +
EMQX boot identically, API tests use `curl -k`:

```bash
bash scripts/tls_stage_rehearsal.sh
```

## 3. Build & first boot (data services first)

```bash
export COMPOSE="docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.vps.yml"
$COMPOSE up -d --build postgres redis
sleep 20
# BUG-1 (QA A5, deploy blocker): the migrate MUST run on the ADMIN DSN.
# On a fresh volume the app role has no CREATE on schema public (PG15+) and
# `daphne`'s own DATABASE_URL connects as the app role -> it dies at
# `alerts.0006` with `permission denied for schema public`. Bootstrap the
# roles FIRST, then migrate as the admin role (the sanctioned path
# scripts/prod_bootstrap.sh itself uses).
# فارسی: اول نقشها ساخته شوند، بعد migrate با نقشِ ادمین اجرا شود؛ اجرای
# migrate با نقشِ اپ روی والیومِ تازه شکست میخورد.
bash scripts/prod_bootstrap.sh   # role passwords + grants + subscriber accounts (MUST run first)
$COMPOSE run --rm -e DATABASE_URL="postgres://${POSTGRES_ADMIN_USER}:${POSTGRES_ADMIN_PASSWORD}@postgres:5432/${DATABASE_NAME}" daphne python manage.py migrate
$COMPOSE up -d --build
docker ps   # all containers Up
```

## 4. Smoke test

```bash
bash scripts/vps_smoke.sh
# with an existing app user, also prove login:
SMOKE_USER=<user> SMOKE_PASSWORD=<pass> bash scripts/vps_smoke.sh
```

Contract probes (no hardware needed):

```bash
python3 scripts/check_hil_readiness.py --skip-live
```

## 5. API test examples

```bash
# Health (self-signed path needs -k; real certs don't)
curl -k -s https://127.0.0.1/api/v1/health/ | head -c 300; echo

# Login -> access/refresh tokens
curl -k -s -X POST https://127.0.0.1/api/v1/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"<user>","password":"<pass>"}'
```

Full endpoint reference: `backend/openapi.yaml` (mirror
`backend/openapi.generated.yaml`). Deployment runbook: `DEPLOYMENT.md`.

## 6. Useful operations

```bash
$COMPOSE logs -f daphne              # API logs
$COMPOSE logs -f subscriber-01       # MQTT bridge logs
bash scripts/walg_base_backup.sh     # wal-g base backup + evidence (needs S3)
sudo bash scripts/vps_health.sh      # one-command health check (read-only, exit 1 on problems)
python3 scripts/ota_checksums.py --help
```

## 7. Notes & limits

- **WAL growth:** with `WALG_S3_PREFIX` empty (short tests), archiving has
  nowhere to push — fine for days, not weeks. Watch it:
  `docker compose -p morghdari exec postgres du -sh /var/lib/postgresql/data/pg_wal`.
  Set real S3 vars for anything longer-lived.
- **Rollback:** re-point at the previous image/tag per `DEPLOYMENT.md §4`;
  migrations are forward-apply only (previous code runs fine on newer schema).
- **Never on the VPS:** `docker-compose.rehearsal.yml`, MinIO, loopback port
  publishes, `MQTT_TLS_INSECURE` beyond staging self-signed tests.
