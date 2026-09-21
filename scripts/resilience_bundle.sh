#!/usr/bin/env bash
# Evidence bundle: the two open criteria, one command, machine-checkable.
# C1 application-running-under-supervision
# C2 boot-persistence-confirmed
set -uo pipefail
{
echo "### C1: running-under-supervision"
echo "docker_service_enabled=$(systemctl is-enabled docker 2>&1)"
echo "docker_service_active=$(systemctl is-active docker 2>&1)"
echo "containers_total=$(docker ps -q | wc -l)"
echo "containers_healthy=$(docker ps --filter health=healthy -q | wc -l)"
docker ps --filter name=morghdari --format 'container:{{.Names}} state:{{.State}} health:{{.Status}}' | sort
echo "restart_policy_daphne=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' morghdari-daphne)"
echo "restart_policy_postgres=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' morghdari-postgres)"
echo "daphne_RestartCount=$(docker inspect -f '{{.RestartCount}}' morghdari-daphne)"

echo
echo "### C1-drill: deliberate crash (kill -9 daphne) and autonomous recovery"
RC_BEFORE=$(docker inspect -f '{{.RestartCount}}' morghdari-daphne)
PID=$(docker inspect -f '{{.State.Pid}}' morghdari-daphne)
echo "kill -9 pid=$PID (RestartCount before=$RC_BEFORE)"
kill -9 "$PID"
sleep 3
docker ps --filter name=morghdari-daphne --format 't+3s: state:{{.State}} status:{{.Status}}'
sleep 10
docker ps --filter name=morghdari-daphne --format 't+13s: state:{{.State}} status:{{.Status}}'
docker inspect -f 'daphne_after: RestartCount={{.RestartCount}} state={{.State.Status}} health={{.State.Health.Status}}' morghdari-daphne
echo -n 'api_after_recovery: '
curl -sk --max-time 10 https://127.0.0.1/api/v1/health/ | head -c 60; echo

echo
echo "### C2: boot persistence proof (this boot)"
echo "uptime_line=$(uptime)"
echo "docker_since_boot_active=$(systemctl is-active docker)"
# All containers Up with uptime == host uptime => started by docker at boot, not manually.
docker ps --filter name=morghdari --format 'boot_recovery:{{.Names}} {{.Status}}' | sort
} > /root/resilience-bundle.txt 2>&1
cat /root/resilience-bundle.txt
rm -f /root/resilience-bundle.txt
