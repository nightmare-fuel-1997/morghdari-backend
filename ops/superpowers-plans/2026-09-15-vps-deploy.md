# VPS Deploy Execution Plan — `vps-02-rc1`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Human executes all VPS steps; agent executes laptop steps.

**Goal:** Current tree → serving production traffic on the VPS in one human-run session, with proven rollback.

**Architecture:** Prep (tag+push) in build mode here; VPS work is a human-run runbook reusing repo scripts, extended only where the repo has gaps (first owner, S3 verification, BUG-12 Linux proof).

**Tech Stack:** Ubuntu 22.04/24.04, Docker Engine + Compose v2, local builds (`vps.yml`), Let's Encrypt Path A, S3-compatible WAL/backups.

**Spec:** `README_VPS.md` §§0–7, `DEPLOYMENT.md` §§1–7, `.env.vps.example`, `scripts/deploy.sh` (auto-rollback: app services only).

## Global Constraints
- S3 bucket available; TLS Path A (DNS ready before deploy day); human executes VPS steps; code travels via a new GitHub remote.
- `demo_seed.py` NEVER runs on the VPS. Migrations are forward-only (rollback re-points code, never DB).
- Sibling coordination first: tree holds their uncommitted work + their `fix/bug-sweep` branch approach (superseded in one file). Agree what ships in the tag.
- No completion claims without fresh evidence.

---

### Phase 0 — Green-gate + tag + push (build mode, laptop)
- [ ] Step 1: sibling-coordination check — `git status --porcelain`, `git branch -a`, `git stash list` in both repos; identify sibling-owned paths; confirm the tag contents.
- [ ] Step 2: full parent `run_acceptance.sh` → 3/3 green on the current tree (covers BUG-13 port + gateway unification landed after the 650 run).
- [ ] Step 3: hygiene — extend `.gitignore` (`scripts/*.log`, `.tmp-qa/`, `qa_*.log`, `coverage.json` if untracked); `git add -A`; verify `git status` shows no logs/scratch staged.
- [ ] Step 4: `git commit -m "release: vps-02-rc1 — QA 296/0/1, all open bugs closed (see BUGS.md §D)"`; `git tag -a vps-02-rc1`; same in parent repo. Verify `git describe --tags`.
- [ ] Step 5: create GitHub remote (both repos — needs human auth if `gh` is not logged in); `git push origin <branch> --tags`; record clone URLs (replace README's `<your-github-url>` placeholder).

### Phase 1 — VPS prereqs (human, ~20 min)
- [ ] Ubuntu 22.04/24.04, 2c/4GB; docker.io + compose plugin + openssl + curl; user in docker group, re-login.
- [ ] Firewall: open 80, 443, 8883 only. From laptop: `nc -zv <VPS-IP> 443` etc.
- [ ] DNS: `dig +short api.<domain>` → VPS IP (API + MQTT hostnames). Not green = stop.

### Phase 2 — Clone + configure (human, ~15 min)
- [ ] `git clone <remote-url> --branch vps-02-rc1 /opt/morghdari && cd /opt/morghdari`; `git describe --tags` → vps-02-rc1.
- [ ] `cp .env.vps.example .env`; fill EVERY value (`openssl rand -hex 32`); `DJANGO_ALLOWED_HOSTS=api.<domain>,daphne`; real `WALG_S3_PREFIX` + `AWS_*`; `CERTIFICATE_EMAIL`; `chmod 600 .env`.

### Phase 3 — TLS Path A (human, ~15 min)
- [ ] Edit `scripts/tls_issue_vps.sh` hostnames if needed; `sudo bash scripts/tls_issue_vps.sh`.
- [ ] Verify `certs/fullchain.pem` + `openssl s_client` dates/subject; confirm renewal timer (`systemctl list-timers | grep -i cert` or crontab).

### Phase 4 — Build + first boot (human, ~25 min)
- [ ] `export COMPOSE="docker compose -p morghdari -f docker-compose.prod.yml -f docker-compose.vps.yml"`; `$COMPOSE up -d --build postgres redis`; sleep 20; both healthy.
- [ ] `bash scripts/prod_bootstrap.sh` (MUST precede migrate).
- [ ] Migrate on admin DSN: `$COMPOSE run --rm -e DATABASE_URL="postgres://${POSTGRES_ADMIN_USER}:${POSTGRES_ADMIN_PASSWORD}@postgres:5432/${DATABASE_NAME}" daphne python manage.py migrate` → exit 0.
- [ ] `$COMPOSE up -d --build`; all Up; nginx reload; `echo "vps-02-rc1" > .deploy_tag`.

### Phase 5 — First owner, no demo seed (human, ~10 min)
- [ ] `createsuperuser` via daphne run (break-glass, sealed storage).
- [ ] Org + owner membership via daphne-shell ORM one-liner (admin DATABASE_URL).
- [ ] Owner login → 200; change password immediately; verify superuser break-glass.

### Phase 6 — Prove it (human, ~30 min)
- [ ] `vps_smoke.sh` 7/7; `check_hil_readiness.py --skip-live`.
- [ ] BUG-12 Linux proof: `docker kill morghdari-daphne` → auto-restart ≤40s? Closes or escalates BUG-12.
- [ ] Prometheus targets: django job up; beat heartbeat fresh. Spot WS close codes (bad token → 101+4003).

### Phase 7 — Backups live (human, ~15 min)
- [ ] `walg_base_backup.sh` → base on YOUR bucket; `backup.sh` artifact + S3 copy; install both crontabs; pg_wal baseline `du`.

### Phase 8 — Go-live + rollback contract
- [ ] GO iff Phases 4–7 green + BUG-12 resolved either way + backup-list non-empty on real S3.
- [ ] Rollback = `deploy.sh <prev-tag>` (app services; DB forward-only — log it).
