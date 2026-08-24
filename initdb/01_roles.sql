-- =============================================================================
-- initdb/01_roles.sql — the FIVE database roles (database.md Compartment 8).
--
-- فارسی: پنج نقش پایگاه‌داده طبق داکیومنت؛ نقش اپ هرگز مالک جداول نیست چون
-- PostgreSQL به مالک و سوپریوزر هیچ‌وقت RLS اعمال نمی‌کند.
--
-- Runs once at FIRST cluster bootstrap (both compose files mount ./initdb).
-- Idempotent via pg_roles guards so ops can safely re-run it on an existing
-- volume. Roles carry NO passwords by design (CONSTRAINTS C-08 family):
-- dev/CI connect through their bootstrap users; the app role's behavior is
-- exercised via SET ROLE in tests/test_rls.py — real app-role URLs with
-- secrets are deployment wiring (Deployment & DevOps Guide §2.1), not v0.1.
--
-- Roles verbatim:
--   morghdari_owner     migration/DDL only — NEVER an app login target
--   morghdari_app       Django (daphne + celery + subscriber) — DML only
--   morghdari_admin     BYPASSRLS platform staff path (Security §4.5)
--   emqx_auth           EMQX: SELECT on credentials/acl/gateway_slots ONLY
--   morghdari_exporter  postgres-exporter monitoring: pg_monitor, no data DML
-- =============================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'morghdari_owner') THEN
    CREATE ROLE morghdari_owner LOGIN;            -- migration/DDL only (no app login)
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'morghdari_app') THEN
    CREATE ROLE morghdari_app   LOGIN;            -- Django runtime — NOT the owner
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'morghdari_admin') THEN
    CREATE ROLE morghdari_admin LOGIN BYPASSRLS;  -- platform staff path (is_platform_staff)
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'emqx_auth') THEN
    CREATE ROLE emqx_auth       LOGIN;            -- EMQX auth reader — minimal grants below
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'morghdari_exporter') THEN
    CREATE ROLE morghdari_exporter LOGIN;         -- monitoring — pg_monitor, no data-table DML
  END IF;
END
$$;

GRANT pg_monitor TO morghdari_exporter;

-- Schema access. PG15+ stopped granting CREATE on public to PUBLIC; the
-- bootstrap user (who RUNS this script) owns future Django tables, so the
-- default-privileges line below follows every later table/index automatically.
GRANT USAGE ON SCHEMA public TO morghdari_app, emqx_auth, morghdari_exporter;

-- App role: full DML on every table the creating role will ever make.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO morghdari_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO morghdari_app;

-- EMQX reader: SELECT on exactly three tables (doc Compartment 8). Guarded —
-- at initdb time Django has not created them yet; re-running after migrate
-- applies them. Default privileges deliberately NOT granted to emqx_auth:
-- new tables stay invisible unless explicitly listed here.
DO $$
BEGIN
  IF to_regclass('public.mqtt_device_credentials') IS NOT NULL THEN
    GRANT SELECT ON mqtt_device_credentials TO emqx_auth;
  END IF;
  IF to_regclass('public.mqtt_acl_rules') IS NOT NULL THEN
    GRANT SELECT ON mqtt_acl_rules TO emqx_auth;
  END IF;
  IF to_regclass('public.gateway_slots') IS NOT NULL THEN
    GRANT SELECT ON gateway_slots TO emqx_auth;
  END IF;
END
$$;
