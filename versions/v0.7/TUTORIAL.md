# v0.7 TUTORIAL — take it to a real VPS yourself

> ⚠️ **Pre-spiral draft** — re-validate at spiral start. **How to use:** 1–4 teach,
> 5 is hands-on, 6 warns, 7 checks. KB lesson: *Infrastructure & Ops* (#/infra).
> Canonical: Deployment & DevOps Guide (§1–8), Observability Runbook, monitoring/
> artifacts in the docs repo.

---

## 1. The Mission

Move the system from your laptop to an internet-facing VPS with TLS, monitoring,
backups, paging, and rollback — then prove all five work by attacking the system on
purpose.

**Done means:** pinned-tag deploy · health 200 · killing the subscriber pages you ·
rollback is one command · support engineer (MFA) works audited · prober shows real
uptime numbers.

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| certbot webroot + deploy-hook | TLS issuance/renewal | Both nginx AND emqx need certs BEFORE boot | Don't use standalone mode — webroot survives renewals cleanly |
| `MORGHDARI_TAG=<sha>` | Pinned image deploys | Rollback = previous tag, one command | Never `latest` in prod compose |
| healthchecks + `stop_grace_period` | Orderly boot/shutdown | Subscriber drains 30 s, worker 60 s, pg 120 s | Not decoration — §8.3 ordering rationale |
| Prometheus + exporters + Grafana | Metrics + dashboards + alerting | The scream-when-sick layer | Loopback only — never expose 3000/9090 |
| `subscriber_up < 2` rule | The T4b page | One dead subscriber must not start the loss clock alone | Not optional pre-customer |
| wal-g / pg_dump `-Fc` | Backup artifacts | Custom format keeps Timescale catalog (`-i` is gone in PG12+ — never pass it) | Never `-t` flag (TTY corrupts dumps) |
| External uptime prober | Outside-view availability | Self-monitoring dies with the VPS | Not a substitute for internal alerts |

---

## 3. The Build

### 3.1 Perimeter first

```
Reachable:      22 (key-only) · 80/443 (nginx) · 8883 (EMQX TLS)
Loopback-bound: 5432 · 6379 · 18083 · 3000 · 9090 · 5555 · 8000
```

Docker-published ports bypass ufw — the BIND address is the control; ufw is
defense-in-depth. SSH tunnels are how you reach every dashboard.

### 3.2 Certificates before boot

nginx and EMQX refuse to start without certs. Run the certbot webroot dance with a
temporary nginx serving ACME challenges, copy issued files under BOTH naming
conventions (`fullchain/privkey/chain.pem` for nginx; `server.crt/key/ca.crt` for
EMQX), install `/etc/letsencrypt/renewal-hooks/deploy/reload-tls.sh` which copies +
hot-reloads both services. Rehearse one renewal — a renewal that fails silently is an
outage in 60 days.

### 3.3 Production compose — read as a security document

```yaml
postgres:
  environment:
    POSTGRES_USER: ${POSTGRES_ADMIN_USER}   # bootstrap SUPERUSER = morghdari_admin, never app role
redis:
  command: redis-server ... --requirepass ${REDIS_PASSWORD}   # password via CLI, never a file
emqx:
  environment:
    EMQX_DB_USER: ${EMQX_DB_USER}           # emqx_auth role ONLY — app vars deliberately absent
daphne/subscriber/celery:
  image: ghcr.io/morghdari/backend:${MORGHDARI_TAG}           # pinned, no build blocks
```

Every line encodes an audit finding: superuser-vs-app separation, Redis AOF always +
noeviction (benchmark gate AU-FSVEC-GATE), EMQX auth isolation, single-image strategy.

### 3.4 Monitoring wiring

Scrape targets: cAdvisor · redis-exporter · postgres-exporter (`morghdari_exporter`
role) · EMQX `/api/v5/prometheus/stats` · Django `/api/v1/metrics/` · subscriber :9091.
Alerts from `monitoring/alerts.yml` via Grafana alerting (no alertmanager Phase 1):
`subscriber_up < 2` CRITICAL · queue drops · pending depth · beat heartbeat · DB
connections ≥ `db_connections_alert_pct`.

### 3.5 Backups that restore

Nightly `pg_dump -Fc` AS ADMIN (catalog access), S3 SSE-KMS verified monthly,
retention 30 d — plus the rehearsal: stop writers → drop → CREATE EXTENSION →
pg_restore → verify hypertables. Log the drill.

### 3.6 Support activation day

Enforce MFA (django-otp TOTP) on platform-staff accounts; deploy the four v1.2 ops
endpoints behind session validation; rehearse the audited suspend on staging. The
audit rows ARE the feature — show them.

### 3.7 Prober + metering

Second-location prober hits `/api/v1/health/` per minute → uptime history + burn-rate
alert (closes MET-AVAIL's blind spot). Cost counters join exporter metrics per tenant →
`cost_per_tenant` panel (MET-SAAS) — the number v1.0's look-back will demand.

---

## 4. Cheat Sheet

| Task | Command | Key detail | Gotcha |
|---|---|---|---|
| Deploy tag N | set TAG in .env → `pull` → `up -d` | pull BEFORE up | prune after, not before |
| Rollback | previous tag + `up -d` | one command | migrations may not reverse — check first |
| Tunnel Grafana | `ssh -L 3000:127.0.0.1:3000 vps` | loopback-only dashboards | never open the port |
| Verify backup encryption | `s3api head-object` | expect `aws:kms` | monthly check |
| Kill subscriber test | `docker compose stop subscriber-01` | 02 absorbs; page fires | full T4b test stops BOTH |
| Restore rehearsal | DR §3.1 steps | admin user; extension first | log elapsed vs RTO |

---

## 5. Your Subtasks

1. **Harden the VPS:** ufw rules + key-only ssh. Verify: external scan shows exactly
   22/80/443/8883.
2. **Issue TLS; boot the stack.** Verify: HTTPS health 200; EMQX listener verifies via
   openssl s_client.
3. **Deploy by pinned tag; break it; roll back.** Verify: one-command rollback healthy.
   Notice: check migration reversibility before rolling back across them.
4. **Stand up monitoring; wire alerts to your phone.** Verify: kill subscriber-01 →
   page received <10 min. Notice: subscriber-02 absorbed traffic meanwhile.
5. **Schedule backups + run the restore rehearsal.** Verify: row counts match; DR log
   entry #1 written.
6. **Activate support role: MFA enroll + audited suspend on staging.** Verify:
   SUPPORT_ORG_ENTER/SUPPORT_OP_* rows exist. Notice: superuser untouched throughout.
7. **Point the external prober at production.** Verify: uptime history accrues;
   burn-rate alert tested by briefly stopping nginx.
8. **Enable cost metering panels.** Verify: per-tenant resource counters visible.
9. **Run the chaos list** (subscriber kill / DLQ spike / disk-fill warning). Verify:
   each fired its intended alert.
10. **DoD sweep: CHANGELOG, tag `v0.7`, demo recorded.**

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| Renewal hook never rehearsed | Site down ~60 days later on expired certs |
| Monitoring stack over budget | OOM kills during traffic spikes |
| Backup without restore rehearsal | Confidence without evidence |
| Opening Grafana/emqx ports "temporarily" | Permanent exposure with weak passwords |
| Rolling back across an irreversible migration | Half-migrated schema, longer outage |

---

## 7. Prove It

1. Why must POSTGRES_ADMIN_USER differ from DATABASE_USER? What silently breaks if
   they're equal? *(§3.3/v0.1 roles.)*
2. Where does EMQX get its database credentials, and why aren't the Django DB vars
   passed to its container? *(§3.3.)*
3. State the T4b two-subscriber rule and what specifically pages you. *(§3.4.)*
4. Your backup ran nightly for a year. Name TWO pieces of evidence proving it was ever
   usable. *(§3.5.)*
5. Why is an external prober required when Prometheus already watches everything?
   *(§3.7/MET-AVAIL.)*
