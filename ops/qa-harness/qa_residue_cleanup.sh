#!/usr/bin/env bash
# QA residue cleanup — deletes leftover qa-farm-*/journey-farm* farms in the
# QA org, with all their dependent rows, in ONE transaction.
#
# WHY a DB-level script: the product's DELETE /farms/{id}/ deliberately
# refuses farms that have saloons or history (RESTRICT philosophy — never a
# cascade). That is correct for production data and exactly why QA farms
# accumulate: every harness/journey run leaves one behind. This script is the
# QA-org-only exception, scoped so tightly it cannot hit anything else:
#
#   1. org_id must be the QA org (hard-coded UUID below)
#   2. farm name must match 'qa-farm-%' or 'journey-farm%' (harness patterns)
#   3. a farm hosting any GW-AUTO-% simulator slot is excluded — the
#      simulator feeds the telemetry the health check watches
#   4. refuses to run if > 500 farms match (something unexpected happened)
#   5. refuses if any victim alert is on legal hold
#   6. takes a FULL pg_dump before the delete (after guards pass)
#   7. exits 0 with "nothing to do" when there is no residue
#
# Also removes the slot's MQTT identity (mqtt_acl_rules + credentials, keyed
# by username with no FK — D21 hygiene), the per-saloon Redis threshold keys,
# and flushes EMQX's authorization cache.
#
# Usage (from the local checkout, key-auth SSH to the VPS):
#   bash ops/qa-harness/qa_residue_cleanup.sh census   # dry run, read-only
#   bash ops/qa-harness/qa_residue_cleanup.sh apply    # guards + backup + delete
set -euo pipefail

SSH_TARGET="${MORGHDARI_SSH:-root@82.115.17.252}"
MODE="${1:-census}"
PG="docker exec -i morghdari-postgres psql -U morghdari_admin -d morghdari -v ON_ERROR_STOP=1"

# Victim selection is IDENTICAL in both modes (single source of truth).
SCOPE_SQL="
create temp table v_farms as
  select f.id, f.name from farms f
  where f.org_id = '359273d4-1c84-42dc-8bdc-fb13512975bd'
    and (f.name like 'qa-farm-%' or f.name like 'journey-farm%')
    and not exists (select 1 from gateway_slots gs
                    where gs.farm_id = f.id and gs.slot_id like 'GW-AUTO-%');
create temp table v_saloons as select id from saloons where farm_id in (select id from v_farms);
create temp table v_slots as select slot_id from gateway_slots where farm_id in (select id from v_farms);
"

if [ "$MODE" = "census" ]; then
  ssh -o BatchMode=yes "$SSH_TARGET" "$PG" <<SQL
$SCOPE_SQL
\pset footer off
select 'FARMS (to delete)' as what, count(*)::text as n from v_farms
union all select 'saloons', count(*)::text from v_saloons
union all select 'gateway_slots', count(*)::text from v_slots
union all select 'saloon_telemetry', count(*)::text from saloon_telemetry where saloon_id in (select id from v_saloons)
union all select 'alert_instances', count(*)::text from alert_instances where farm_id in (select id from v_farms)
union all select 'command_logs', count(*)::text from command_logs where farm_id in (select id from v_farms)
union all select 'daily_reports', count(*)::text from daily_reports where farm_id in (select id from v_farms)
union all select 'installation_jobs', count(*)::text from installation_jobs where farm_id in (select id from v_farms)
union all select 'device_units', count(*)::text from device_units where slot_id in (select slot_id from v_slots)
union all select 'gateway_commands', count(*)::text from gateway_commands where gateway_slot_id in (select slot_id from v_slots)
union all select 'mqtt_acl_rules', count(*)::text from mqtt_acl_rules where username in (select slot_id from v_slots)
union all select 'mqtt_device_credentials', count(*)::text from mqtt_device_credentials where username in (select slot_id from v_slots)
union all select 'flock_cycles', count(*)::text from flock_cycles where saloon_id in (select id from v_saloons)
union all select 'account_activity_logs', count(*)::text from account_activity_logs where farm_id in (select id from v_farms) or saloon_id in (select id from v_saloons)
union all select 'saloon_sensor_slots', count(*)::text from saloon_sensor_slots where saloon_id in (select id from v_saloons)
union all select 'remaining_farms_in_org', count(*)::text from farms where org_id = '359273d4-1c84-42dc-8bdc-fb13512975bd' and id not in (select id from v_farms);
SQL
  exit 0
fi

if [ "$MODE" != "apply" ]; then
  echo "usage: $0 [census|apply]" >&2; exit 2
fi

# ---- apply ------------------------------------------------------------------

echo "== 1/5 guards =="
N_FARMS="$(ssh -o BatchMode=yes "$SSH_TARGET" "docker exec morghdari-postgres psql -U morghdari_admin -d morghdari -t -A -c \"select count(*) from farms f where f.org_id = '359273d4-1c84-42dc-8bdc-fb13512975bd' and (f.name like 'qa-farm-%' or f.name like 'journey-farm%') and not exists (select 1 from gateway_slots gs where gs.farm_id = f.id and gs.slot_id like 'GW-AUTO-%');\"" | tr -d '[:space:]')"
echo "victim farms: $N_FARMS"
if [ "$N_FARMS" = "0" ]; then
  echo "no residue — nothing to do."
  exit 0
fi

ssh -o BatchMode=yes "$SSH_TARGET" "$PG" <<SQL
$SCOPE_SQL
do \$\$
declare
  n_farms int; n_sim int; n_hold int;
begin
  select count(*) into n_farms from v_farms;
  select count(*) into n_sim from gateway_slots
    where slot_id like 'GW-AUTO-%' and farm_id in (select id from v_farms);
  select count(*) into n_hold from alert_instances
    where legal_hold and farm_id in (select id from v_farms);
  if n_farms > 500 then raise exception 'GUARD: % farms matched (cap 500) — unexpected scope, refusing', n_farms; end if;
  if n_sim > 0 then raise exception 'GUARD: % simulator slots would be affected — refusing', n_sim; end if;
  if n_hold > 0 then raise exception 'GUARD: % legal-hold alerts among victims — refusing', n_hold; end if;
  raise notice 'guards ok: % farms, simulator slots 0, legal holds 0', n_farms;
end \$\$;
SQL

echo "== 2/5 fresh backup (pruning old cleanup dumps, keep 7d) =="
STAMP="$(date +%Y%m%d-%H%M%S)"
DUMP="/backups/postgres/qa_cleanup_pre_${STAMP}.dump"
ssh -o BatchMode=yes "$SSH_TARGET" "find /backups/postgres -name 'qa_cleanup_pre_*.dump' -mtime +7 -delete; docker exec morghdari-postgres pg_dump -U morghdari_admin -d morghdari -Fc -f '$DUMP' && ls -la '$DUMP'"

echo "== 3/5 delete (single transaction) =="
ssh -o BatchMode=yes "$SSH_TARGET" "$PG" <<SQL
begin;
$SCOPE_SQL
-- children of saloons (telemetry + aggregates first: the big ones)
delete from saloon_telemetry          where saloon_id in (select id from v_saloons);
delete from metric_hourly_aggregates  where saloon_id in (select id from v_saloons);
delete from metric_daily_aggregates   where saloon_id in (select id from v_saloons);
delete from alert_daily_stats         where saloon_id in (select id from v_saloons);
delete from daily_reports             where farm_id in (select id from v_farms) or saloon_id in (select id from v_saloons);
delete from alert_acknowledgements    where alert_id in (select id from alert_instances where farm_id in (select id from v_farms));
delete from alert_instances           where farm_id in (select id from v_farms);
delete from command_logs              where farm_id in (select id from v_farms);
-- children of gateway slots
delete from gateway_commands          where gateway_slot_id in (select slot_id from v_slots);
delete from device_photos             where gateway_slot_id in (select slot_id from v_slots);
delete from provisioning_audit_log    where gateway_slot_id in (select slot_id from v_slots);
delete from gateway_hardware_registry where slot_id in (select slot_id from v_slots);
delete from device_units              where slot_id in (select slot_id from v_slots);
-- MQTT identity of the deleted slots: ACL rules and credentials are keyed by
-- username (= slot_id) with NO FK — deleting the slot alone orphans them
-- (D21 hygiene family). They must go in the same transaction.
delete from mqtt_acl_rules           where username in (select slot_id from v_slots);
delete from mqtt_device_credentials  where username in (select slot_id from v_slots);
delete from installation_jobs         where farm_id in (select id from v_farms) or gateway_slot_id in (select slot_id from v_slots);
delete from gateway_saloon_zones      where saloon_id in (select id from v_saloons) or gateway_slot_id in (select slot_id from v_slots);
delete from gateway_requests          where farm_id in (select id from v_farms);
-- saloon-scoped history
delete from doctor_tickets            where saloon_id in (select id from v_saloons);
delete from support_tickets           where saloon_id in (select id from v_saloons);
delete from flock_feed_intake         where saloon_id in (select id from v_saloons);
delete from flock_water_intake        where saloon_id in (select id from v_saloons);
delete from flock_weight_samples      where saloon_id in (select id from v_saloons);
delete from flock_daily_snapshots     where saloon_id in (select id from v_saloons);
delete from mortality_records         where saloon_id in (select id from v_saloons);
delete from maintenance_mutes         where saloon_id in (select id from v_saloons);
delete from actuator_hardware_registry where slot_id in (select slot_id from saloon_actuator_slots where saloon_id in (select id from v_saloons));
delete from saloon_actuator_slots     where saloon_id in (select id from v_saloons);
delete from saloon_operational_states where saloon_id in (select id from v_saloons);
delete from saloon_photos             where saloon_id in (select id from v_saloons);
delete from medical_prescriptions     where flock_id in (select fc.flock_id from flock_cycles fc where fc.saloon_id in (select id from v_saloons));
delete from flock_cycles              where saloon_id in (select id from v_saloons);
-- farm-scoped history + linkage
delete from lifecycle_threshold_rules where template_id in (select t.template_id from lifecycle_templates t where t.farm_id in (select id from v_farms));
delete from lifecycle_templates       where farm_id in (select id from v_farms);
delete from biosecurity_events        where farm_id in (select id from v_farms);
delete from account_role_scopes       where farm_id in (select id from v_farms) or saloon_id in (select id from v_saloons);
delete from account_activity_logs     where farm_id in (select id from v_farms) or saloon_id in (select id from v_saloons);
delete from legacy_slug_map           where farm_id in (select id from v_farms) or saloon_id in (select id from v_saloons);
-- the rows themselves, children first
delete from gateway_sensor_mapping    where slot_id in (select slot_id from saloon_sensor_slots where saloon_id in (select id from v_saloons));
delete from hardware_registry         where slot_id in (select slot_id from saloon_sensor_slots where saloon_id in (select id from v_saloons));
delete from saloon_positions          where saloon_id in (select id from v_saloons);
delete from saloon_sensor_slots       where saloon_id in (select id from v_saloons);
delete from saloons                   where farm_id in (select id from v_farms);
delete from gateway_slots             where farm_id in (select id from v_farms);
delete from farms                     where id in (select id from v_farms);
commit;
SQL

echo "== 4/5 Redis per-saloon keys + EMQX authz cache =="
ssh -o BatchMode=yes "$SSH_TARGET" 'PW=$(grep -E "^REDIS_PASSWORD=" /opt/morghdari/.env | cut -d= -f2-)
# every saloon:*:thresholds key whose saloon id no longer exists is stale
docker exec morghdari-redis sh -c "redis-cli -a \"$PW\" --no-auth-warning --scan --pattern \"saloon:*:thresholds\"" > /tmp/sk.txt
docker exec morghdari-postgres psql -U morghdari_admin -d morghdari -t -A -c "select id from saloons" > /tmp/live_ids.txt
kept=0; removed=0; del_args=""
while read -r k; do
  sid=${k#saloon:}; sid=${sid%%:*}
  if grep -qx "$sid" /tmp/live_ids.txt; then kept=$((kept+1));
  else removed=$((removed+1)); del_args="$del_args $k"; fi
done < /tmp/sk.txt
if [ -n "$del_args" ]; then
  docker exec morghdari-redis sh -c "redis-cli -a \"$PW\" --no-auth-warning DEL $del_args" > /dev/null
fi
echo "redis threshold keys: kept=$kept removed=$removed"
rm -f /tmp/sk.txt /tmp/live_ids.txt
docker exec morghdari-emqx emqx ctl cache clean 2>/dev/null \
  && echo "emqx cache flushed" \
  || echo "(emqx cache clean unavailable — non-fatal; cached authz entries expire on their own)"'

echo "== 5/5 verify =="
ssh -o BatchMode=yes "$SSH_TARGET" "$PG" <<'SQL'
select 'farms_left_in_org=' || count(*) from farms where org_id = '359273d4-1c84-42dc-8bdc-fb13512975bd';
select 'residue_farms_left=' || count(*) from farms where org_id = '359273d4-1c84-42dc-8bdc-fb13512975bd' and (name like 'qa-farm-%' or name like 'journey-farm%');
select 'active_tier1_alerts=' || count(*) from alert_instances where current_status = 'ACTIVE_TIER_1';
select 'simulator_slots_ok=' || count(*) from gateway_slots where slot_id like 'GW-AUTO-%';
select 'true_orphan_mqtt_ids=' || count(*) from mqtt_device_credentials c
  where c.username like 'GW-%'
    and not exists (select 1 from gateway_slots g where g.slot_id = c.username)
    and not exists (select 1 from device_units d where d.unit_id = c.username);
SQL
echo "done. backup at $DUMP on the VPS"
