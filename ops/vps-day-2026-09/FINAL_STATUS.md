# FINAL STATUS — Morghdari Deployment to 82.115.17.252

**Date:** 2026-09-17 (Asia/Tehran) · **Status: COMPLETE and VERIFIED** · Operator: AutoCoder
**Live check at close:** `https://82.115.17.252/api/v1/health/` → `200 {"status":"healthy","version":"0.8-DR",...}` (fresh external connection after firewall hardening, 0.51 s)

## 1. What was asked vs. what was delivered

| # | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Deploy project to 82.115.17.252 (root) | ✅ Done | 9 containers: nginx, emqx, redis, postgres, daphne, subscriber-01/02, celery-worker, celery-beat — all `healthy`, deploy dir `/opt/morghdari`, images `backend:vps-01` + `postgres-walg:vps-01` built on-server |
| 2 | Running and healthy | ✅ Done | Health endpoint healthy since 14:32 server time; DB ok (24–57 ms), Redis ok (2.4–6 ms); 78 migrations; subscribers seeded |
| 3 | APIs reachable from outside | ✅ Done | From my machine every round + check-host.net independent nodes (DE/BR/NL HTTP 200, TCP 443 4/4): health `200`, login `401/400` JSON, 404s clean, `/api/v1/metrics/` blocked (404) |
| 4 | Survives crash | ✅ Done (5 drills) | `kill -9` on daphne host-PID → auto-restart healthy in ~13 s each time; `RestartCount` climbed 0→4 across the day |
| 5 | Comes back by itself after reboot | ✅ Done (2 real reboots) | Both self-recovered, zero manual commands; container uptimes == host uptime; data (org, 2 slots, 2 MQTT creds, 78 migrations) intact |
| 6 | Traceable record | ✅ Done | Ledger rows 1–24 in `/opt/morghdari/DEPLOYMENT_LEDGER_2026-09-17.md` (+ local copy) |
| 7 | Rollback path | ✅ Documented | `HANDOVER_2026-09-17.md` — tag-repoint rollback, `down -v` teardown, VPS #1 (185.8.175.150) untouched as fallback |
| 8 | Secrets & exposure | ✅ Done + hardened | `.env` 0600, no placeholders, keys 0640 root:gid1000; **ufw now active** (default deny incoming; allow 22/80/443/8883 only; 18083 host-blocked) |
| 9 | Work preserved in git | ✅ Done | `e9023e1` on `origin/main`: BUG-25 deferred-grant fix in `scripts/prod_bootstrap.sh` + new one-shot `scripts/fresh_vps_bringup.sh`; resilience bundle persisted as `scripts/resilience_bundle.sh` |

## 2. What remains — ONLY items that require you

| # | Item | Why you are needed | Where to start |
| --- | --- | --- | --- |
| 1 | Point a domain at 82.115.17.252 | I cannot buy or control DNS for you. Ports 80/443 are open and the ACME webroot is wired; once DNS resolves, `scripts/tls_issue_vps.sh` issues the real Let's Encrypt cert (until then clients need `-k`/skip-verify on the self-signed staging cert) | Your DNS provider |
| 2 | Rotate the root password & move to key-only SSH | The password you pasted in chat was used once (to install my deploy key) and should be considered burned. Rotating it and disabling password auth is an irreversible credential change only you should authorize | Send me a new password, or say "disable password SSH" and I'll do it key-only |

Nothing else is pending, blocked, or unverified on my side.

## 3. How to verify anything yourself (no trust required)

```bash
ssh root@82.115.17.252 "bash /opt/morghdari/scripts/resilience_bundle.sh"   # supervision + crash drill + boot proof
curl -sk https://82.115.17.252/api/v1/health/                               # external health (from anywhere)
ssh root@82.115.17.252 "ufw status verbose"                                 # firewall: active, 22/80/443/8883 only
```

## 4. Risks / open items (documented, non-blocking)

- Staging self-signed TLS cert in use (by design until a domain exists).
- WAL/S3 archiving intentionally emptied (laptop MinIO was the old target) — set real S3 vars before production data lands.
- BUG-27 dormant: `EmqxQueueDropped` alert references a metric EMQX 5.5.1 doesn't export — monitoring gap only.
- Docker published ports bypass ufw's INPUT chain (Docker FORWARD-chain behavior); the *actual* public surface is still exactly 22/80/443/8883 because compose only publishes those (+18083 loopback-only), verified externally post-firewall.

## 5. Rollback / teardown (recovery path)

App rollback: re-point compose at a previous image tag, `up -d` (migrations forward-only).
Full teardown: `cd /opt/morghdari && docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.vps.yml down -v`, then delete `/opt/morghdari`.
Firewall off (if ever needed): `ufw disable`.

## 6. Artifact index

| Artifact | Location |
| --- | --- |
| Deployment ledger (24 rows) | VPS `/opt/morghdari/DEPLOYMENT_LEDGER_2026-09-17.md` |
| Handover + risks | VPS `/opt/morghdari/HANDOVER_2026-09-17.md` |
| This report | VPS `/opt/morghdari/FINAL_STATUS.md`; local `DELIVERY/FINAL_STATUS_2026-09-17.md` |
| Bring-up script (next fresh host) | repo `scripts/fresh_vps_bringup.sh` (git `e9023e1`) + VPS copy |
| Resilience evidence | VPS `/root/resilience-bundle-latest.txt`, regenerable |

---

## UPDATE 2026-09-18 — REAL TLS LIVE (the last self-service task)

- Domains: **https://farmplus.romaksystem.com** (API/admin/WS) and **mqtt.farmplus.romaksystem.com:8883** (MQTT TLS) — both A-records point at 82.115.17.252.
- Let's Encrypt certificate issued 2026-09-17: CN farmplus.romaksystem.com, SAN [farmplus.romaksystem.com, mqtt.farmplus.romaksystem.com], issuer LE (YE1), **expires 2026-12-16**, auto-renewal via certbot systemd timer + deploy hook (restages to /opt/morghdari/certs with EMQX-readable perms 0640 root:gid1000, hot-reloads nginx + EMQX listener). `certbot renew --dry-run` = SUCCESS.
- `.env` updated: DJANGO_ALLOWED_HOSTS += both domains; CERTIFICATE_EMAIL=admin@romaksystem.com. Backups: `.env.bak-20260917-203605`, `nginx/nginx.conf.bak-20260917-203605`.
- Verified externally (no -k anywhere): health 200 over valid TLS; http:// 301 -> https://; login endpoint 400 JSON; MQTT 8883 openssl Verify return code 0 (ok) from VPS host AND from operator laptop; renewal dry-run success.
- Client connection settings: REST base `https://farmplus.romaksystem.com/api/v1`; WS `wss://farmplus.romaksystem.com/ws/`; MQTT `mqtt.farmplus.romaksystem.com:8883` with standard system trust store (the old self-signed ca.crt workaround is no longer needed for external clients).
- Remaining user-only: rotate the VPS root password / move to key-only SSH (deploy key `autoclaw-deploy` already authorized).
