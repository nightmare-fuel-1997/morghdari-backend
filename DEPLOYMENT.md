# Deployment Runbook — Morghdari Backend

> One page, decision-oriented. Written for the on-call engineer at 02:00.
> Companion: `scripts/run_acceptance.sh` (the acceptance ritual) ·
> `task_plan.md` errors table (lessons that produced the rules below).

## 1. The environment contract (twelve-factor, fail-fast)

| Variable | Required | Notes |
| --- | --- | --- |
| `DATABASE_URL` | YES everywhere (prod raises `RuntimeError` at boot without it — no defaults since 2026-09-06) | `postgres://…@host:5432/db` |
| `DJANGO_SECRET_KEY` | YES in prod (KeyError-by-design) | — |
| `DJANGO_ALLOWED_HOSTS` | YES in prod (comma-separated; empty ⇒ raise) | — |
| `REDIS_URL` | required by all services that touch Redis | dev-credential fallback is tracked debt |
| `DJANGO_SETTINGS_MODULE` | containers set it (`config.settings.production`) | `manage.py` defaults to development |

All three settings modules verified 2026-09-06: dev check green, prod
fail-fast proven, test absent-maps-to-sqlite.

## 2. Pre-deploy ritual (MANDATORY, one command)

```bash
bash scripts/run_acceptance.sh
```

Runs, in order: **migrate-gate** (deployability of the migration graph) →
**test** (pytest + coverage gate) → **image-scan** (trivy CRITICAL/HIGH,
fixable CVEs). All three must be green before any tag/deploy. The script
hardcodes the two flags that keep it honest:

- `-p morghdari_ci` — without a project name compose replaces the DEV stack's
  containers (C-13; it bit again 2026-09-06 — the dev :5432 postgres came up
  with CI credentials; data survived only via the named volume).
- `--build` — without it you validate a stale image (it once reported a
  pre-M6 suite as "green"). Green means green NOW.

For the EMQX milestone (broker authn/authz/rotation semantics), also run the
real-broker gate:

```bash
docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build live-verify
```

## 3. Deploy sequence (rolling, ASGI)

1. Build/tag the image from the exact tree that passed the ritual
   (`git rev-parse HEAD` recorded in the change ticket).
2. Run `manage.py migrate` on the new environment **before** switching
   traffic — the migrate-gate proved it is forward-safe, idempotent, and
   (informationally) reversible per app.
   On a FRESH volume this migrate must run with the ADMIN DSN after
   `scripts/prod_bootstrap.sh` created the roles - see "Migrate on a fresh
   volume (BUG-1)" below; the app role has no CREATE on schema public (PG15+).
3. Roll workers (Daphne/ASGI behind nginx) instance by instance; health check
   between instances (image HEALTHCHECK probes `/api/v1/health/`: DB + Redis).
4. Restart the MQTT subscriber; watch it reconnect (2 s retry loop, FNV-soft).
5. Mount the photo volume on every backend instance:
   `media_prod:/app/media` (the image creates the dir appuser-owned; without
   the mount O6 uploads die with the container and `default_storage.url()`
   rows point at files that no longer exist after a re-deploy).

## 4. Rollback procedure

```bash
# Application: re-point to the previous image tag (kept for every release).
docker service update --image <registry>/morghdari:<previous-tag> backend
# or single host:
docker stop backend && docker run -d --name backend <registry>/morghdari:<previous-tag>

# Verification (required before declaring the rollback done):
curl -fsS http://localhost:8000/api/v1/health/ | grep '"status"'
docker ps   # subscriber + backend up, EMQX reachable
```

- **Migrations are forward-apply only**: rolling back the CODE is safe
  (previous image runs fine on newer schema); rolling the SCHEMA back needs
  the per-app reversibility notes the migrate-gate prints — never assume it.
- Feature-level containment: new endpoints are additive; a bad v1.6/v1.7
  path can be disabled at nginx without a rollback.

## 5. Incident first-response (02:00 card)

1. `curl http://<host>/api/v1/health/` — `status: ok` vs `degraded` names the
   failing probe (DB / Redis).
2. Telemetry stopped but health OK → check the subscriber first (broker down
   shows in EMQX dashboard; pipeline is FNV-soft, so silence = consumer dead,
   not broker dead).
3. WebSocket storms → `ws:active` cap closes 4000; auth errors close 4003 —
   a spike of 4003 means a bad client build, not an outage.
4. DLQ depth growing → look at `gateway_rejected`/`gateway_suspended` reasons
   (provisioning state, not ingestion bugs).
5. Never hand-edit the database; every schema change goes through a migration
   (the migrate-gate exists precisely because of what escapes review).

## 6. Edge limits live in nginx, not Django (02:00 card, part 2)

Django deliberately carries only the ONE `auth_strict` throttle (O10, 3/min).
Coarse limits belong to nginx so junk never reaches Python. Minimum zones for
the v0.7 VPS bring-up (the full file lands with the prod compose):

```nginx
limit_req_zone $binary_remote_addr zone=auth:10m rate=5r/s;
limit_req_zone $binary_remote_addr zone=gwcfg:10m rate=1r/s;
limit_req_zone $binary_remote_addr zone=compat:10m rate=5r/s;

location /api/v1/auth/               { limit_req zone=auth   burst=10 nodelay; proxy_pass http://backend; }
location = /api/v1/gateways/config/ { limit_req zone=gwcfg  burst=2  nodelay; proxy_pass http://backend; }
location /api/compat/                { limit_req zone=compat burst=10 nodelay; proxy_pass http://backend; }
# WebSocket: Django closes 4003 (auth/org) BEFORE 4000 (cap) — a 4003 spike
# means a bad client build, not an outage; never log ?token= ($uri only).
```


## Migrate on a fresh volume (BUG-1)

On a fresh volume the app role (`morghdari_app`) has no `CREATE` on schema
`public` (PG15+), so the first `manage.py migrate` must run with an
admin-equivalent URL - the same sanctioned path as `scripts/prod_bootstrap.sh`
(the app-role migrate dies at `alerts.0006` with `permission denied for
schema public`):

```bash
DATABASE_URL=postgres://<admin>:<pw>@<host>:5432/<db> \
  docker compose -f docker-compose.prod.yml -p morghdari run --rm daphne \
  python manage.py migrate
```

The CI migrate-gate uses admin-equivalent credentials, so it does NOT catch
this - use the admin URL for first-time migration.

---

## 7. VPS-day daphne kill drill (BUG-12, RESOLVED 2026-09-16 on 185.8.175.150)

**Resolved on the VPS, and the drill itself was wrong.** Three controlled tests
on Ubuntu 24.04 / Docker 29.7.2:

| test | result |
| --- | --- |
| `docker kill morghdari-daphne` | container stays `exited`, RestartCount 0 — **by design**: a user-initiated stop suppresses the restart policy on every platform, so this drill can never pass |
| `docker exec -u root morghdari-daphne sh -c 'kill -9 1'` | no effect, `kill` exits 0 — a PID-namespace init ignores signals sent from inside its own namespace |
| `kill -9 $(docker inspect -f '{{.State.Pid}}' morghdari-daphne)` | `die` -> `start` in **4 s**, RestartCount 1, health healthy, API 200 |

So `restart: always` works; the desktop observation was the drill, not the
engine. Use the host-PID form:

```bash
kill -9 $(docker inspect -f '{{.State.Pid}}' morghdari-daphne)
# expect: status=restarting -> running within seconds, RestartCount +1
# gap traffic: 502/429 bounded, never an infinite timeout (nginx still answers)
```

- Verdict: closed as a drill-methodology issue (4 s auto-restart measured). No
  watchdog was added - the engine already does the job.
- Still worth fixing in the *application*: a container that is killed and comes
  back has an empty in-memory state, and nginx keeps proxying to the old IP
  until a reload (see BUG-14 in scripts/deploy.sh).

---

## 8. The repository and the two checkouts (sync procedure)

Until 2026-10-14 the local dev tree and the VPS tree were two unconnected copies,
which is why every change had to be hand-reconciled. Both are now checkouts of a
single repository.

| | Path | Branch |
| --- | --- | --- |
| Local dev | `M:\projects\poultry_the_main_by_ai` | `main` |
| VPS | `/opt/morghdari` | `main` |

Remote: `git@github.com:nightmare-fuel-1997/morghdari-backend.git` (private).

**Server authentication.** A read-write *deploy key* lives at
`/root/.ssh/morghdari_deploy`, selected by `/root/.ssh/config`. It is scoped to
this one repository. Revoke it from repo Settings → Deploy keys.

**`core.fileMode = false` on the VPS.** The repository was authored on Windows,
where the executable bit is not recorded. Without this setting three scripts
(`backend/run_full.bat`, `scripts/fresh_vps_bringup.sh`,
`scripts/resilience_bundle.sh`) show as permanently modified because the VPS has
them `chmod +x` and the repo has them `100644`.

### Deliberately NOT in the repository

These exist only on the VPS and are matched by `.gitignore`:

```
.env   .env.bak-*   .sim.env
certs/          (incl. privkey.pem, server.key — TLS private keys)
certbot-www/    *.log    *.bak-*    /backups/
```

> **Never run `git add -A` on the VPS without checking `.gitignore` first.** The
> rules in place before 2026-10-14 did not cover `.env`, `.sim.env` or `certs/`,
> so that command would have committed the TLS private keys and the database
> password. The current file covers all of them; verify with
> `git check-ignore -v .env certs/privkey.pem .sim.env`.

### Syncing

Local → VPS:

```bash
git push origin main
ssh root@82.115.17.252 'cd /opt/morghdari && git pull --ff-only && git status --short'
```

VPS → local:

```bash
ssh root@82.115.17.252 'cd /opt/morghdari && git add -A && git commit -m "<what changed>" && git push origin main'
git pull --ff-only
```

`git status --short` must come back empty on both sides. If it does not, the two
trees have drifted again — stop and reconcile before deploying.

### Two kinds of change, and they deploy differently

1. **Deploy layer** — compose files, `nginx/`, `emqx/`, `redis/`, `monitoring/`,
   `scripts/`. A `git pull` puts the files in place; `scripts/deploy.sh <tag>` or
   a compose `up -d` then applies them.
2. **Application code** — `backend/`. This does **not** run from the tree. Every
   backend service uses a pre-built image,
   `ghcr.io/morghdari/backend:${MORGHDARI_TAG}`. A source change needs a new image
   built and pushed, then deployed by tag. Editing `backend/` on the VPS and
   restarting containers changes nothing.

### Known live issue at the time of writing (2026-10-14)

The `vps-02` tag was moved to a new build on 2026-10-14, and `daphne` plus
`simulator` were recreated onto it — but `celery-worker`, `celery-beat`,
`subscriber-01` and `subscriber-02` were left on the previous image
(`668229220018`, built 2026-09-21), which no longer exists in the image store.
The web tier is therefore running newer code than the async and MQTT tiers.
Recreating those four containers settles it.

