# Deployment Ledger — morghdari → 82.115.17.252

All times Asia/Tehran (UTC+03:30). Operator: AutoCoder (goal b6f348a2).
Target: `82.115.17.252` (root, password supplied by user; SSH key `autoclaw-deploy` installed 13:38).
Source tree: `M:\projects\poultry_vps_deploy` @ git `f7ac8ec` (+ uncommitted VPS-day fixes per README_VPS/DEPLOYMENT).

| # | Time (UTC+3:30) | Step | Command (abridged) | Result |
|---|---|---|---|---|
| 1 | 13:37 | TCP/SSH reachability | ssh banner probe | OK — ED25519 host key added |
| 2 | 13:38 | Password auth via SSH_ASKPASS | ssh mkdir /root/.ssh | OK — `AUTH-OK` |
| 3 | 13:39 | Install operator SSH key | ssh-keygen ed25519 + authorized_keys append | OK — `KEY-INSTALLED`, BatchMode (key-only) works |
| 4 | 13:39 | Host identity + resources | hostname/uname/os-release/nproc/free/df | srv1661423174, Ubuntu 24.04.5 LTS, x86_64, 2 vCPU, 3.8 GiB RAM, 48 GB disk (2.5 GB used) |
| 5 | 13:39 | Docker install | apt-get install docker.io docker-compose-plugin | Docker 29.1.3 + Compose v2.40.3 installed |
| 6 | 13:45 | Stack upload prep | tar archive 4.1 MB (excl .git, .tmp-qa, .opencode, projects.lnk, logs) | OK |
| 7 | 14:05 | Upload + extract to /opt/morghdari | scp + tar -xf | OK — full tree on server |
| 8 | 14:20 | .env on VPS (scp, 0600) + ALLOWED_HOSTS+=82.115.17.252; WALG_S3_PREFIX emptied (laptop MinIO unreachable) | sed | OK |
| 9 | 14:20 | Base pulls (nginx/emqx/redis/python/timescale) + local build backend:vps-01 + postgres-walg:vps-01 | compose build --pull | OK — BUILD-RC=0; pypi/dockerhub/github reachable |
| 10 | 14:25 | BUG-25 fix applied to prod_bootstrap.sh (deferred grants/seeds); BUG-24 key perms 0640 root:gid1000; data services up | bootstrap pass1 | OK — exit 0, grants deferred |
| 11 | 14:30 | Migrate as ADMIN (BUG-1 path) | compose run daphne migrate | OK — MIGRATE-RC=0 (incl alerts.0006) |
| 12 | 14:32 | Bootstrap pass 2 (mqtt grants + subscriber-01/02 seeds) + full stack up | bootstrap + up -d | OK — 9 containers, org ac1d1186 created; nginx/beat/subs healthy t+11s |
| 13 | — | TLS staging rehearsal (IP-only path B) | bash scripts/tls_stage_rehearsal.sh | pending |
| 14 | — | Smoke test (in-band) | bash scripts/vps_smoke.sh | pending |
| 15 | — | External API reachability (from laptop) | curl -k https://82.115.17.252/api/v1/health/ etc. | pending |
| 16 | — | Crash-recovery drill (BUG-12 host-PID form) | kill -9 $(docker inspect …) → auto-restart | pending |
| 17 | — | Boot persistence (reboot) | reboot → re-check containers | pending |
| 18 | — | Port exposure audit | ss/nft/ufw vs intended 22/80/443/8883 | pending |

Secrets: root password used once for key install, then never again; all secrets live only in `/opt/morghdari/.env` on the VPS (0600). Ledger deliberately contains no secrets.
| 13 | 13:50 | Crash drill (BUG-12 host-PID form) | kill -9 $(docker inspect ...daphne) | OK — RestartCount 1, healthy t+9s, API 200 post-recovery |
| 14 | 13:41 | External API probes from laptop over public internet | curl -k https://82.115.17.252/... | OK — health 200 healthy (IP + SNI paths), 80→301, login 401 JSON, metrics 404 (blocked), 8883 open, 18083 closed |
| 15 | 13:55 | 404 body leak check | grep urlconf/traceback/DEBUG | OK — no debug leak |
| 16 | 13:56 | Port audit | ss -tlnp + ufw status | OK — public 22/80/443/8883 only; 18083 loopback; ufw inactive (cloud-level) |
| 17 | 14:00 | Reboot persistence | reboot; docker is-enabled=enabled | OK — 9/9 containers healthy at t+5min, zero manual starts |
| 18 | 14:03 | Post-reboot external re-check | curl from laptop | OK — health 200 healthy, login 401, 8883 TLS up |
| 19 | 14:05 | Data persistence across reboot | psql counts | OK — org=1, gateway_slots=2, mqtt creds=2, migrations=78 (pre-reboot data intact) |
| 20 | 14:42 | Crash drill RE-RUN for evidence pack (host-PID kill -9) | kill -9 $(docker inspect …daphne) | OK — RestartCount 0→1, starting t+3s → healthy t+13s, API healthy post-recovery |
| 21 | 14:43 | Reboot RE-RUN for evidence pack | /sbin/reboot | OK — docker active, 9/9 healthy at t+1min, zero manual starts |
| 22 | 14:47 | Independent external confirmation (check-host.net) | HTTP GET /api/v1/health/ from 4 nodes + TCP 443 from 4 nodes | OK — HTTP 200 from DE/BR/NL (0.06-0.64s); IN timed out (17s, single-node upstream routing gap, 3/4 OK); TCP 443 4/4 OK; local curl healthy post-reboot |
| 23 | 14:50 | Evidence bundle C1+C2 (single-command re-verification) | /tmp/bundle.sh | OK — docker enabled+active, 9/9 healthy+running, restart=always, kill-9 drill RestartCount 0→1 healthy t+13s API ok; boot proof: containers Up==host uptime 7min |
| 24 | 17:24 | Host firewall enabled (last self-service hardening item) | ufw allow 22/80/443/8883 + ufw --force enable | OK — active, default deny incoming, SSH session survived; external re-verify: health 200 healthy 0.51s, login 400, 18083 host-blocked, 8883/443 open |
| 25 | 2026-09-17 21:06 | TLS issuance (Let's Encrypt) | farmplus.romaksystem.com + mqtt.farmplus.romaksystem.com issued via certbot webroot through the LIVE nginx (no downtime); SAN covers both names; nginx server_name switched (commit 1de71bc); EMQX ssl listener hot-restarted; renewal deploy hook installed (LIVE=/etc/letsencrypt/live/farmplus.romaksystem.com, CERTS=/opt/morghdari/certs); certbot renew --dry-run SUCCESS; expires 2026-12-16, auto-renews via certbot.timer | DONE | health 200 valid-TLS from laptop; HTTP->HTTPS 301; login 400 JSON; MQTT 8883 openssl verify=0 (ok) host+laptop; privkey 0640 root:gid1000 |
