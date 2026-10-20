-- =============================================================================
-- initdb/02_rls.sql — ENABLE + FORCE + tenant_isolation policy over EVERY
-- org-scoped table (database.md Compartment 8, AU-RLS 2026-08-16).
--
-- فارسی: روی هر جدول سازمان‌دار سه خط اعمال می‌شود؛ جدول‌های global عمداً
-- سیاست ندارند (TUTORIAL §3.9) و همین تفاوت توسط اسکریپت parity چک می‌شود.
--
-- WHY guarded: initdb scripts run BEFORE Django creates any table, so every
-- statement is a no-op at bootstrap; the SAME file is re-run post-migrate by
-- the RLS Django migration (M1-06), ops, and tests/test_rls.py fixtures.
-- Idempotent: DROP POLICY IF EXISTS before each CREATE POLICY.
--
-- Policy template verbatim: current_setting(..., true) returns NULL when the
-- GUC is unset → comparison is NULL → zero rows = FAIL-CLOSED for morghdari_app.
-- FORCE binds the table OWNER too (only superuser/BYPASSRLS escape).
--
-- Org-less globals deliberately have NO policy (no org_id column to filter):
-- provinces, cities, villages, sensor_metric_types, system_configuration,
-- tier_quota_mapping, mqtt_device_credentials, mqtt_acl_rules.
-- =============================================================================

DO $$
DECLARE
  t text;
  tenant_tables text[] := ARRAY[
    -- C1 tenancy/access
    'organizations',
    'org_members',
    'account_role_scopes',
    -- C2 edge/topology
    'farms',
    'saloons',
    'gateway_requests',
    'gateway_slots',
    'gateway_hardware_registry',
    'gateway_saloon_zones',
    'saloon_positions',
    'saloon_sensor_slots',
    'hardware_registry',
    'gateway_sensor_mapping',
    'saloon_photos',
    'saloon_actuator_slots',
    'actuator_hardware_registry',
    -- C3 telemetry/time-series — the two HYPERTABLES are deliberately ABSENT
    -- here: TimescaleDB rejects ENABLE RLS after columnstore is enabled
    -- (CONSTRAINTS C-11), so their policies are applied by the telemetry DDL
    -- migration BEFORE compression setup: apps/telemetry/migrations/
    -- 0002_timescale_hypertables.py. flock_cycles is a regular table.
    'flock_cycles',
    -- C4 alerting/config (+ D22/D23)
    'lifecycle_templates',
    'alert_instances',
    'alert_acknowledgements',  -- C-10 resolution: gains org_id, joins the policy universe
    'saloon_operational_states',
    'doctor_tickets',
    'mortality_records',
    'medical_prescriptions',
    'flock_weight_samples',
    'flock_feed_intake',
    'flock_water_intake',
    'biosecurity_events',
    'maintenance_mutes',
    'alert_subscriptions',  -- v0.9 M9-10/B2: org-wide category mutes (GS-ROUTING)
    'user_panel_overrides',  -- v0.9 M9-08/B3: per-user panel gates (definitions + role rows are global, like the metric catalog)
    'dashboard_configs',
    'provisioning_audit_log',
    'org_offboarding_log',
    -- C5 commands
    'command_logs',
    -- Phase 0 (2026-10-18): MQTT-native provisioning surfaces.
    -- device_units is deliberately ABSENT: it is the org-less factory
    -- inventory (AD-1), the same posture as mqtt_device_credentials.
    'installation_jobs',
    'gateway_commands',
    'device_photos',
    -- C6 audit
    'account_activity_logs'
  ];
BEGIN
  FOREACH t IN ARRAY tenant_tables LOOP
    IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
      EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
      EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I '
        'USING (org_id = NULLIF(current_setting(''app.current_org_id'', true), '''')::UUID) '
        'WITH CHECK (org_id = current_setting(''app.current_org_id'', true)::UUID)',
        t
      );
    END IF;
  END LOOP;
END
$$;

-- C-10 RESOLVED (owner decision 2026-08-24): alert_acknowledgements gained
-- org_id in database.md (doc-first) and is a full policy member above.
-- Related nuance (unchanged): lifecycle_templates.org_id is NULLable — NULL-org
-- global templates are invisible to tenant contexts under the equality policy.

-- v0.7 M7-03 (laptop rehearsal finding): the EMQX postgresql authn backend
-- JOINs gateway_slots as emqx_auth and NEVER sets app.current_org_id, so the
-- FORCE'd tenant_isolation policy returned zero rows for EVERY authn attempt
-- (fail-closed "Not authorized"). The exemption is deliberately minimal:
-- SELECT-only, emqx_auth-only, gateway_slots-only. Tenant isolation for all
-- other roles is untouched. Twin of tenants/0003_emqx_auth_read_policy.py
-- (which runs where migrations — not initdb — create the tables).
DO $$
BEGIN
  IF to_regclass('public.gateway_slots') IS NOT NULL THEN
    DROP POLICY IF EXISTS emqx_auth_read ON gateway_slots;
    CREATE POLICY emqx_auth_read ON gateway_slots
        FOR SELECT TO emqx_auth
        USING (true);
  END IF;
END
$$;

-- 2026-09-11 (rehearsal finding): a login/refresh request carries NO org
-- claim yet, so tenant_isolation hid EVERY org_members row from morghdari_app
-- and login always died with "no active org membership". The exemption is
-- deliberately minimal: SELECT-only, morghdari_app-only, own-row-only
-- ("user" = app.current_user_id, set by resolve_active_membership from the
-- already-authenticated principal — never client input, tx-scoped). Writes
-- stay governed by tenant_isolation. Twin of
-- tenants/0004_orgmember_self_lookup.py.
DO $$
BEGIN
  IF to_regclass('public.org_members') IS NOT NULL THEN
    DROP POLICY IF EXISTS org_members_self_lookup ON org_members;
    CREATE POLICY org_members_self_lookup ON org_members
        FOR SELECT TO morghdari_app
        USING ("user" = NULLIF(current_setting('app.current_user_id', true), '')::integer);
  END IF;
END
$$;

-- v1.7.1 (2026-09-12, found while building the template write surface): the
-- tenant_isolation comparison `org_id = current_setting('app.current_org_id')`
-- can never be true for a NULL org_id, so the org-NULL BUILT-IN catalog was
-- invisible under enforced RLS while superuser (dev/CI) saw it plainly. Harmless
-- while templates were seeded-only; load-bearing now that the resolution chain
-- ends at the global default and O4 must show the built-in rows. Minimal
-- exemption: SELECT-only, morghdari_app-only, org_id IS NULL rows only —
-- tenant-owned rows of other orgs stay invisible and no write path is granted.
-- Twin of tenants/0005_lifecycle_templates_global_read.py.
DO $$
BEGIN
  IF to_regclass('public.lifecycle_templates') IS NOT NULL THEN
    DROP POLICY IF EXISTS lifecycle_templates_global_read ON lifecycle_templates;
    CREATE POLICY lifecycle_templates_global_read ON lifecycle_templates
        FOR SELECT TO morghdari_app
        USING (org_id IS NULL);
  END IF;
END
$$;
