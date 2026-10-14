# Operations Log — 2026-09-19 (SaaS health, admin, simulator)

Operator: AI maintenance session. All changes below are deployed live unless
marked otherwise. Secrets are NOT stored here — passwords were handed over in
chat; gateway MQTT passwords live in `/opt/morghdari/.sim.env` (mode 600).

## 1. External access & Swagger — verified
- nginx `0.0.0.0:80/443` healthy; `farmplus.romaksystem.com` → `82.115.17.252`;
  Let's Encrypt cert valid 2026-09-17 → 2026-12-16.
- APIs publicly reachable: `/api/v1/health/` 200, `/api/docs/` 200,
  `/api/schema/` 200, `/api/redoc/` 200, `/admin/` 302, HTTP→HTTPS redirect on.
- Swagger: `https://farmplus.romaksystem.com/api/docs/`

## 2. Superuser & roles
- Created `mostafa` (superuser+staff), then bootstrapped owner membership in
  `Morghdari Platform`. No `systemadmin` user exists (platform axis is
  `is_platform_staff` + `staff_role`, not a username).
- Role model: no `Role` table — role = string on `OrgMember`/`AccountRoleScope`
  (enum: owner, manager, engineer, doctor, worker, installer, reviewer).
  Grants go through owner-only API (`POST /org/members/`,
  `PATCH /org/members/{id}/`) or Django admin (see §3).
- Test users created (all in `Morghdari Platform`, usable passwords in chat):
  `frontend-tester` (owner + installer scope = skeleton key),
  `sim-manager/engineer/doctor/worker/installer/reviewer` (one role each).
- `support-ops` (platform staff, `staff_role=support`, MFA confirmed) — the
  only support-surface actor; superusers are rejected there by design.

## 3. Django admin — two fixes
- **CSS 404**: `STATIC_ROOT` pointed at ephemeral `/app/config/staticfiles`
  while the volume/nginx serve `/app/static` (plus root-owned volume vs
  `appuser` runtime, plus no `collectstatic` in image). Fixed
  `config/settings/base.py` (`STATIC_ROOT=/app/static`, absolute
  `STATIC_URL`) and `backend/Dockerfile` (mkdir/chown + bake `collectstatic`).
- **Empty admin**: all 14 `admin.py` were placeholders. Added
  `apps/common/admin_support.py` (`BypassRLSModelAdmin` routes admin ORM
  through the BYPASSRLS `admin` alias — request traffic stays RLS-enforced)
  and registered **69 models**; custom `UserAdmin` with platform fields.
  Godmode stays admin-exclusive; no API god-claim was added (deliberate).

## 4. Simulator + seed data (opt-in, prod untouched)
- Topology via sanctioned API order: farms 2–4 (Sim A/B/C), saloons 2–7,
  6 positions, 6 sensor slots, 3 hardware rows; gateways `GW-SIM-01`,
  `GW-SIM-F3`, `GW-SIM-F4` request→review→sync→approve → all PROVISIONED
  with real bcrypt creds + postgres ACLs.
- `simulator/sim.py` + `docker-compose.sim.yml` (start: add
  `-f docker-compose.sim.yml up -d simulator`). Publishes schema-2.0
  telemetry every 10 s; verified broker → Redis → `saloon_telemetry`.
- Support chain proven: session open + suspend/resume round-trip.

## 5. Bugs found & fixed during the "clock" audit
1. `review_gateway_request` 404 for staff (org-less + RLS hid rows).
   Fix: `admin_alias()` discover + `tenant_context` scope (`provisioning/api.py`).
2. `provisioning/sync` 500 on documented `[{"saloon_id"}]` zones (view assumed
   ints). Fix: accept both, 400 on garbage.
3. EMQX mgmt client dead (loopback URL + Basic auth; 5.5 needs JWT login).
   Fix: `EMQX_API_URL=http://emqx:18083` + dashboard creds + token auth
   (`mqtt_auth/services.py`, compose env).
4. Built-in-DB user/ACL pushes targeted a chain that exists only in dev
   (prod is postgres-only by design). Fix: chain probe + skip; suspend/reject
   now delete DB allow-rows, resume regenerates them (`mqtt_auth/services.py`,
   `provisioning/services.py`). Proven: suspended gateway CONNACK-refused,
   its farm's data frozen, resume restored 9 ACL rows.
5. Simulator `(unhealthy)` from inherited daphne HEALTHCHECK — disabled.
6. **WAL archiving failed 7,570×** (no storage backend; `WALG_S3_PREFIX` empty),
   `pg_wal` at 7.9 GB. Stopgap: `archive_command=/bin/true` via ALTER SYSTEM
   (reload-only, reversible) — `pg_wal` back to ~80 MB. **No PITR backups
   exist; needs an S3 target to configure WAL-G properly.**

## 6. Open / watch items
- `POST /admin/accounts/user/add/` 500 seen once (pre-recreate, traceback lost);
  user-add reproduced fine after (302). Re-report with form contents if seen.
- `MQTT_TLS_INSECURE=1` on subscribers (staging posture, pre-existing).
- `ssl_stapling` nginx warning (cert has no OCSP URL; harmless).
- Rotation flow exercised (rotate+ack verified on all 3 sim gateways).

## 7. Deploy notes (learned the hard way)
- Recreating `daphne` changes its container IP; nginx caches upstream IPs —
  **always `nginx -s reload` after recreating backend services**, else 502s.
- `docker compose -p morghdari -f docker-compose.prod.yml
  -f docker-compose.vps.yml` is the base; add `-f docker-compose.sim.yml`
  only for the simulator. Image tag: `ghcr.io/morghdari/backend:vps-01`.
