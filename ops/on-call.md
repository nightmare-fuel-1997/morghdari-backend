# On-call runbook — v1.0 GA prerequisite

**Status:** ACTIVE 2026-10-28 · **Owner:** whoever holds the rota week
**Scope:** the production VPS (`82.115.17.252`, project `morghdari`)

This is deliberately short. If it needs more than five minutes to read at 3 a.m.,
it will not be read.

---

## 1. The rota

| Week | Primary | Secondary |
|---|---|---|
| (fill on the Friday before) | | |

Rules:
- One primary per week, one secondary as backstop. Swap in writing (this table)
  — never verbally only.
- The primary answers within **30 minutes** during 08:00–23:00, within
  **2 hours** overnight, unless the alert is SEV-1 (then always 30 minutes).
- If the primary cannot be reached in 15 minutes, the secondary takes over and
  the miss is recorded below.

## 2. What actually pages you

Today: **the 6-hourly health-check report** (automation "Morghdari system health
check") is the paging signal. It runs `ops/health-check.py` — 15 phases — and
reports `ALL GREEN` or the exact failing phase.

There is no pager integration yet (v1.1 scope). Until then, the report lands in
the owner's WorkBuddy session four times a day; a failed phase must be treated
as a page.

Alert classes, worst first:

| Class | Example | First response |
|---|---|---|
| **SEV-1 — data path down** | `containers all healthy` fails, API health ≠ 200, telemetry age > 10 min | 30 min, all hands |
| **SEV-2 — degraded** | DLQ keys > 0, worker tracebacks, disk > 85 %, journey red | 2 h |
| **SEV-3 — drift/noise** | drill/journey failure that reproduces as a fixture problem | next business day |

## 3. First five minutes (in this order)

```bash
ssh root@82.115.17.252
cd /opt/morghdari
docker compose -p morghdari ps                 # 1. what is down
curl -sk https://127.0.0.1/api/v1/health/      # 2. app alive?
docker logs --tail 50 morghdari-daphne         # 3. why not
docker logs --tail 50 morghdari-celery-worker
docker logs --tail 50 morghdari-subscriber-01
```

Decision table:

| Symptom | Do this |
|---|---|
| one app container unhealthy | `docker compose -p morghdari --env-file .env -f docker-compose.prod.yml up -d --no-build <service>` |
| postgres/redis unhealthy | same command for that service; verify `docker inspect -f '{{json .State.Health}}'` afterwards |
| everything down after a deploy | `bash scripts/deploy.sh` auto-rolled back — check `/opt/morghdari/.deploy_tag` and `docker inspect -f '{{.Config.Image}}'` per container |
| disk > 85 % | `du -sh /backups/* /var/lib/docker/*` — prune old images, never the WAL archive |
| telemetry stale but containers healthy | check the simulator: `docker logs --tail 20 morghdari-simulator` |

**Never** in an incident: `docker compose down -v`, `docker system prune -a`
(removes images the running stack needs), editing files under `backend/` on the
server (they do not take effect — services run the pinned image).

## 4. Escalation

1. Primary (this runbook, 5 min triage).
2. Secondary (owns the rollback decision).
3. Owner — any SEV-1 longer than 30 minutes, any data-loss suspicion, any
   decision that costs money.

Rollback is always available and always cheaper than debugging forward at 3 a.m.:
`bash /opt/morghdari/scripts/deploy.sh --tag <previous-tag>` (auto-rolls back on
a failed health gate).

## 5. Backups — what exists, and what to do with it

| Artifact | Where | Cadence | Retention |
|---|---|---|---|
| Logical dump (`pg_dump -Fc`) | `/backups/postgres/` | daily 02:00 | 30 days |
| Physical base backup (`pg_basebackup -Ft -z`) | `/backups/base/<date>/` | weekly Sun 03:00 | 28 days |
| WAL archive (gzip) | `/backups/wal/` | continuous (≤5 min lag) | 30 days |

All three are **on the same host** — that protects against corruption and human
error, not against losing the VPS. Offsite (S3 via `walg_base_backup.sh`) is
blocked on credentials; until they exist, copy `/backups` off-host weekly by
hand and say so in the handover.

Restore drill (run it quarterly, log the result):

```bash
# logical: into a scratch DB
docker exec -i morghdari-postgres pg_restore -U morghdari_admin -d morghdari_restore_drill < /backups/postgres/<newest>.dump
# physical PITR: untar the newest base into a scratch volume, then replay WAL
# with restore_command = 'gunzip -c /backups/wal/%f.gz > %p' (plain .gz-less
# segments from 2026-10-28 exist too: try %f first, then %f.gz)
```

## 6. Incidents log

| Date | Class | What happened | Time to recover | Follow-up |
|---|---|---|---|---|
| 2026-10-26 | SEV-2 | redis healthcheck drifted (CI compose merge) — container functional, unhealthy | 20 min | rule: CI gate runs under `-p morghdari_ci` |
| 2026-10-28 | — | WAL archiving was `/bin/true` (no PITR) + no backup cron | fixed proactively | this runbook §5 |
