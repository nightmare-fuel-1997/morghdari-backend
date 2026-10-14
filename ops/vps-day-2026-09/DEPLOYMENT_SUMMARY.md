# Deployment Summary — 82.115.17.252 (2026-09-17)

**Verdict: LIVE and HEALTHY.** All 10 acceptance criteria hold with evidence. The stack survived three kill-crash drills and two full reboots across the day, and APIs answer from outside the server.

## One-line status
`https://82.115.17.252/api/v1/health/` → `200 {"status":"healthy","version":"0.8-DR",...}` (verified repeatedly from an external machine and from 3 independent EU/SA check-host.net nodes).

## Evidence map (what to run to re-verify, on the VPS)

| Claim | Command | Expected |
| --- | --- | --- |
| Under supervision, crash-proof | `ssh root@82.115.17.252 "bash /tmp/bundle.sh"` | 9/9 healthy; kill -9 drill: RestartCount +1, healthy t+13s |
| Boot persistence | same bundle, after any reboot | container uptimes == host uptime |
| External reachability | `curl -sk https://82.115.17.252/api/v1/health/` | `status: healthy` |
| Auth API | POST `/api/v1/auth/login/` with junk creds | 401 JSON, not a proxy error |
| MQTT TLS | `nc -z 82.115.17.252 8883` | open (EMQX ssl:default running, 2 subscribers) |
| Full audit trail | `cat /opt/morghdari/DEPLOYMENT_LEDGER_2026-09-17.md` | 23 ledger rows, all steps + results |

## What was deployed
Tree `f7ac8ec` + VPS-day fixes → built on-server as `ghcr.io/morghdari/backend:vps-01` + `postgres-walg:vps-01`; 9 containers (nginx/emqx/redis/postgres/daphne/subscriber-01/02/celery-worker/celery-beat) under `restart: always`, Docker `enabled` at boot. Migrations: 78. Bootstrap: platform org + both subscriber credentials + ACLs.

## Fixed during this deployment (now in git `e9023e1`, pushed to origin/main)
- **BUG-25 re-applied in `scripts/prod_bootstrap.sh`** — fresh-volume abort at unconditional emqx-table grants; now `to_regclass`-guarded with deferred seeds, exits 0 pre-migrate.
- **NEW `scripts/fresh_vps_bringup.sh`** — the entire proven sequence as one idempotent command for the next fresh host (deps → certs/env sanity → build → data-first → bootstrap ×2 → admin migrate → stack → smoke).

## Acceptance criteria — all 10 satisfied
1. **Server access** — key-based SSH as root, host identity recorded (srv1661423174, Ubuntu 24.04.5, 2c/3.8G).
2. **Running under supervision** — `restart=always` + Docker enabled; 3× kill -9 drills each recovered autonomously (~13 s to healthy).
3. **Boot persistence** — 2 real reboots; stack self-recovered both times; container uptime == host uptime.
4. **Public API reachability** — health 200 (IP + SNI paths), login 401/400 JSON, 404s clean (no debug leak), 3/4 check-host.net nodes HTTP 200 (IN-node timeout = that node's upstream routing), TCP 443 4/4.
5. **Health endpoint healthy** — DB ok (23–57 ms), Redis ok (2–6 ms) from inside and outside.
6. **Data persistence** — org/gateway_slots/mqtt creds/migrations all present after reboots.
7. **Ledger maintained** — rows 1–23 on the VPS + local copy.
8. **Rollback path** — documented in HANDOVER (tag-repoint + `down -v` teardown); recovery path exercised via reboots.
9. **Secrets & exposure** — `.env` 0600, no change-me, key files 0640, only 22/80/443/8883 public, 18083 loopback-only, no secrets in any log/ledger.
10. **External confirmation** — check-host.net reports + local curls, post-reboot.

## Deliberately left for the user (needs user-only inputs/decisions)
- Point a domain at the IP → run `scripts/tls_issue_vps.sh` (webroot on 80 is ready) to replace the self-signed cert.
- Rotate the root password + disable password SSH (deploy key already installed).
- Optional: ufw/provider firewall for defense-in-depth; real S3 vars for WAL archiving before production data lands.

## Files
- VPS: `/opt/morghdari/DEPLOYMENT_LEDGER_2026-09-17.md`, `/opt/morghdari/HANDOVER_2026-09-17.md`, `/opt/morghdari/scripts/fresh_vps_bringup.sh`
- Repo (committed `e9023e1`, pushed): `scripts/prod_bootstrap.sh` (BUG-25 fix), `scripts/fresh_vps_bringup.sh`
