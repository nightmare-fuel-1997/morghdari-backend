# Production vs Local — drift report
**Date:** 2026-10-14 · **Host:** 82.115.17.252 (srv1661423174, Ubuntu 24.04.5)
**Production tree:** `/opt/morghdari` · **Deploy tag:** `vps-02`
**Local:** `M:\projects\poultry_the_main_by_ai`, branch `fix/bug-sweep-2026-09-15` @ `2f60729` (2026-09-19)

---

## Verdict

**Production is ahead of local in every single file that differs. Not one file is
newer locally.** The checked-out branch is roughly three weeks stale.

Production is *not* a git checkout (no `.git`), so its exact commit is unprovable —
but file-by-file checksums place it precisely:

| Bucket | Count |
|---|---|
| Files compared on both sides | 357 |
| Byte-identical | 334 |
| **Different content** | **23** |
| Only in local repo | 87 |
| Only in production | 108 |

Of the 23 differing files:

- **15 match the local branch `merge/local+server-2026-09-21` (`557099b`) exactly** — a branch that exists locally, is **not checked out**, and is **not pushed** (origin only has `fix/bug-sweep-2026-09-15`).
- **8 match neither branch** — they are newer than anything in the local repo.

Production ≈ `merge/local+server-2026-09-21` **plus** a further unversioned change set.

---

## 1. The unversioned change set (exists only on production)

This is the part at risk — it has no commit anywhere.

**New feature: WebSocket protocol docs** — verified live, `HTTP 200` on both:

| Path | Local | Production |
|---|---|---|
| `backend/apps/telemetry/ws_docs_views.py` | absent | present |
| `backend/apps/telemetry/templates/ws_docs.html` | absent | present (123 lines) |
| `backend/tests/test_ws_docs.py` | absent | present |
| `GET /api/ws-docs/` | — | **HTTP 200** (4,694 B) |
| `GET /api/ws-schema/` | — | **HTTP 200** (2,203 B) |

Rationale from the production source: drf-spectacular only introspects DRF HTTP
views, so Swagger covered REST only while the live sockets are Channels consumers.
These two endpoints document the WS protocol instead of faking a Swagger path.

**Modified to support it / other prod-only edits:**

| File | Production change |
|---|---|
| `backend/config/urls.py` | adds `/api/ws-docs/` + `/api/ws-schema/` routes |
| `backend/apps/common/schema_hooks.py` | keeps the two new paths in the OpenAPI filter |
| `backend/config/settings/base.py` | Swagger description points at the WS docs |
| `backend/openapi.yaml` | description line: WS documented via the two endpoints |
| `backend/openapi.generated.yaml` | regenerated mirror (+19 lines) |
| `backend/tests/test_openapi_schema.py` | `KEPT_PREFIXES` extended |
| `nginx/nginx.conf` | real domains `farmplus.romaksystem.com` + `mqtt.farmplus.romaksystem.com` (local still has the `api.morghdari.com` placeholder) |
| `docker-compose.sim.yml` | `SIM_INTERVAL_SEC` `10` → `60` |

**BUG-2 defensive fix** (production only, in the unversioned set):
`backend/apps/monitoring/ops_services.py` — `if day is None: day = tehran_yesterday()`,
guarding a `TypeError` → `500` path that any caller could trigger.

---

## 2. The 15 files covered by the unpushed merge branch

`BUGS.md`, `DEPLOYMENT.md`, `backend/morghdari/subscriber/main.py`,
`docker-compose.prod.yml`, `nginx/nginx.conf.vps`, and 4 test files
(`test_bugfix_2026_09_15.py`, `test_m6_06_ws.py`, `test_m6_06_ws_redis_failopen.py`,
`test_m6_09_compat.py`), plus 5 ops scripts (`pitr_rehearsal.sh`, `prod_bootstrap.sh`,
`redis_wipe_drill.sh`, `reload-tls.sh`, `tls_issue_vps.sh`, `walg_base_backup.sh`).

Commit `557099b` "merge: combine the local and the server fix sets into one evolved
version" (2026-09-21) already captures all of these — 26 files, +1498/−99. The
server-side work is **not lost**; it is simply sitting on a branch nobody checked out.

Notable content in it: BUG-23 (`tls_issue_vps.sh` could never run under `sudo` —
`env_reset` dropped `CERTIFICATE_EMAIL`), BUG-24 (EMQX could not read `privkey.pem`
at `0600 root:root`, so every MQTT TLS handshake was silently dropped while the
listener still reported `running` and healthchecks stayed green), and the BUG-12
daphne kill-drill correction (use the host-PID form; `restart: always` works).

---

## 3. Production-only files that are *not* drift

- **90 QA-harness scripts** — `scripts/qa_*.py`, `qa_*.sh`, `qa_results.jsonl`,
  `qa-test-plan.html`. Snapshot-specific drivers for the live stack; deliberately
  not in the repo per `MIGRATION-2026-09-16-vps-deploy.md`.
- **VPS deploy layer** — `README_VPS.md`, `docker-compose.vps.yml`,
  `backend/scripts/ci_wait_db.py`, `nginx/nginx.conf.vps`, `scripts/fresh_vps_bringup.sh`,
  `vps_health.sh`, `vps_smoke.sh`, `resilience_bundle.sh`, `tls_issue_vps_dns.sh`,
  `demo_seed.py`.
- **Ops-day documents** — `DEPLOYMENT_LEDGER_2026-09-17.md`, `DEPLOYMENT_SUMMARY.md`,
  `FINAL_STATUS.md`, `HANDOVER_2026-09-17.md`, `OPERATIONS_LOG_2026-09-19.md`.
- `nginx/nginx.conf.bak-20260917-203605` — a stray backup, safe to ignore.

## 4. Local-only files that are correctly *not* deployed

87 files: `versions/` (52), `docs/` (17), `monitoring/` (4), and root working notes
(`AGENTS.md`, `CHANGELOG.md`, `CONSTRAINTS.md`, `HANDOFF.md`, `PROGRESS.md`,
`VERSIONS.md`, `MIGRATION-*.md`, `findings.md`, `task_plan.md`,
`baseline-recovery.patch`), `docker-compose.dev.yml`, `docker-compose.rehearsal.yml`,
`.env.example`, `scripts/start_dev_stack.sh`. Production carries only what it runs.

---

## 5. Separate finding: production is running two different builds

Not a repo-drift issue, but found during the audit and worth acting on.

`ghcr.io/morghdari/backend:vps-02` now points at `c9046ae8274e`, built
**2026-10-14 08:07 UTC (today)**:

| Container | Image ID | Container created |
|---|---|---|
| `morghdari-daphne` | `c9046ae8274e` (new) | 2026-10-14 08:08 |
| `morghdari-simulator` | `c9046ae8274e` (new) | 2026-10-14 11:38 |
| `morghdari-celery-worker` | `668229220018` (**old**) | 2026-09-21 21:06 |
| `morghdari-celery-beat` | `668229220018` (**old**) | 2026-09-21 21:06 |
| `morghdari-subscriber-01` | `668229220018` (**old**) | 2026-09-21 21:06 |
| `morghdari-subscriber-02` | `668229220018` (**old**) | 2026-09-21 21:06 |

The old image `668229220018` **no longer exists in the image store** — it was
dropped when the `vps-02` tag was moved to today's build, and only the four running
containers keep it alive.

Consequence: the web tier serves today's code while **async tasks (celery) and MQTT
ingestion (subscribers) still run the 2026-09-21 build.** Any fix shipped in today's
image is inactive on those two tiers.

Also verified: containers have **no source bind-mount** (only `static`/`media`
volumes), so the code is baked into the image — and `/opt/morghdari/backend`
checksums match the running container exactly, so the host tree is a faithful copy
of what is deployed.

Other services are fine: `postgres` (`postgres-walg:vps-02`), `redis`, `emqx`,
`nginx` are all on their expected images, all healthy.

---

## Decisions this raises

1. **Recover the unversioned change set.** The WS-docs feature, the BUG-2 guard,
   the real nginx domains and the sim interval exist only as loose files on the
   host. Either pull them back into the repo and commit, or accept that they are
   deploy-only and record that decision.
2. **Check out / push `merge/local+server-2026-09-21`.** 15 files of server-side
   work are already captured there but unreachable to anyone else.
3. **Roll the worker tier.** `docker compose up -d` on `/opt/morghdari` would move
   celery + subscribers onto today's image and end the split-build state.
