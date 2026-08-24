# v0.7 TASKS — ordered breakdown

| # | Task | Depends on | Acceptance |
|---|---|---|---|
| M7-01 | VPS provisioned per Deployment Guide §10 (4c/16 GB/100 GB SSD); ufw + docker-port discipline (§3.1) | v0.6 | only 22/80/443/8883 reachable |
| M7-02 | TLS issuance runbook executed (certbot webroot; certs copied under BOTH naming conventions; deploy-hook reloads nginx+emqx) | M7-01 | HTTPS live; EMQX 8883 TLS verified |
| M7-03 | Production compose up (all 9 containers incl. subscriber-02, healthchecks, stop_grace_periods) | M7-02 | `docker compose ps` all healthy; health endpoint 200 |
| M7-04 | Monitoring stack (loopback-only) + alerts.yml rules wired to Grafana alerting | M7-03 | dashboards render; test rule fires |
| M7-05 | Backup cron + purge/archive scripts honoring `legal_hold` (DR §5) + restore rehearsal (as admin user; extension pre-create; verify hypertables) | M7-03 | restore drill succeeds on staging; held rows survive purge |
| M7-06 | Shared-subscription rebalance verified (subscriber-02 already ships in the base compose — AU-SUBSCRIBER-N2 closed 2026-08-23) | M7-03 | kill 01 → 02 absorbs; no data-loss clock |
| M7-07 | Support activation: staff MFA enforced; 4 support-op endpoints live behind session | v0.2 | audited suspend demo works; superuser untouched |
| M7-08 | External uptime prober (≥2 regions, 60 s) + burn-rate alerts per Observability §4.2; per-tenant cost metering counters | M7-04 | SLO page has real numbers (MET-AVAIL/MET-SAAS) |
| M7-09 | Chaos drill: kill subscriber/DLQ spike/rollback-one-command | all | page fires; rollback <5 min |
| M7-10 | DoD sweep: CHANGELOG, tag `v0.7`, demo recorded, docs swept | all | tag pushed |
