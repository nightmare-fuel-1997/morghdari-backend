# v0.7 — Real VPS + Observability *(M7)*

> **Theme:** the system runs on real hardware and screams when it's sick.
> **Risk killed:** operations.
> **Maps to:** Blueprint **M7** · **Duration:** ~2 weeks

## What this version delivers

- Production compose live on the VPS: nginx (TLS, rate limits), EMQX (TLS-only 8883,
> dashboard loopback), Redis (AOF always, requirepass via CLI), postgres tuned
- TLS issuance runbook (certbot webroot, deploy-hook hot-reload for nginx+EMQX)
- Monitoring stack: Prometheus + cAdvisor + redis/postgres exporters + EMQX metrics +
> Grafana + Loki + Flower, all loopback; alert rules wired (subscriber_up < 2 CRITICAL,
> queue drops, pending depth, beat heartbeat, DB ≥95 %)
- Backup scripts + cron (pg_dump -Fc as admin user, S3 SSE-KMS, retention)
- subscriber-02 (the T4b safeguard — ships in the base compose; M7 verifies the
> shared-subscription rebalance)
- **Support-role activation + MFA for platform-staff accounts + the 4 v1.2 support-op
> endpoints** (GS-SUPPORT)
- **External uptime prober** for the 99.9 % API SLO + per-tenant cost metering
> (MET-AVAIL/MET-SAAS)
- Chaos drill as a first-class exercise

## Exit demo

Deploy from CI by pinned tag · health 200 · kill the subscriber → the
`subscriber_up < 2` page fires · DLQ spike drill · rollback to previous tag in one
command · support engineer (MFA) enters an org via audited session and suspends a
gateway — superuser untouched.

## Files

[TASKS.md](TASKS.md) · [WALKTHROUGH.md](WALKTHROUGH.md) · [TUTORIAL.md](TUTORIAL.md)

## Look-back

Disk growth vs the capacity model (AU-DISK-MATH) — real numbers now? Did any SLO need
re-baselining after real traffic?
