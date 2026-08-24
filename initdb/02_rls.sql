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
    'saloon_operational_states',
    'doctor_tickets',
    'mortality_records',
    'medical_prescriptions',
    'flock_weight_samples',
    'flock_feed_intake',
    'flock_water_intake',
    'biosecurity_events',
    'maintenance_mutes',
    'dashboard_configs',
    'provisioning_audit_log',
    'org_offboarding_log',
    -- C5 commands
    'command_logs',
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

-- KNOWN GAP (CONSTRAINTS C-10): alert_acknowledgements IS tenant data but the
-- doc gives it NO org_id column, so the mechanical 1:1 parity rule excludes it
-- here — meaning app-role reads of ack rows are NOT row-filtered. Owner decision
-- pending: doc-first fix (add org_id per the doc's own AU-RLS invariant) would
-- flow into this list, the Django model, and parity automatically.
-- Related nuance: lifecycle_templates.org_id is NULLable — NULL-org global
-- templates are invisible to tenant contexts under the equality policy.
