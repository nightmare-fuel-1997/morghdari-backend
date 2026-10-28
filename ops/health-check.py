#!/usr/bin/env python3
"""Morghdari full-system health check — one command, one verdict.

Runs the complete verification sequence against the LIVE deployment:

  1. INFRA    — containers healthy, API health 200, beat dispatching,
                worker/subscriber error-free, telemetry fresh, DLQ empty.
  2. FIXTURES — reactivate the QA probe users (they are deactivated by the
                drill's own cleanup at the end of every run).
  3. ALERTS   — fire two real alerts through the product's own firing service
                (the drill's lifecycle checks need OPEN alerts, and the
                recovery sweep resolves them within minutes — so fire
                immediately before the drill, never earlier).
  4. DRILL    — role_probe_drill.py (56 checks: auth, role gates, tenant
                isolation, data plane, alert lifecycle, template rules,
                sensor slots, member state machine).
  5. JOURNEY  — journey_e2e.py (25 steps: full farm lifecycle + device
                provisioning + REAL MQTT connect → desired → APPLIED →
                PROVISIONED → telemetry visible via REST).
  6. HARNESS  — api_actions.py (85 checks: every write surface end-to-end).

Exit code 0 only when every phase is green.

Usage:
    MORGHDARI_SSH_PASS=... python ops/health-check.py [--skip-harness]

Env:
    MORGHDARI_SSH_PASS   root password for the VPS (required for phases 1-3)
    MORGHDARI_BASE       base URL (default https://82.115.17.252)
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS_DIR = os.path.join(HERE, 'qa-harness')
BASE = os.environ.get('MORGHDARI_BASE', 'https://82.115.17.252')
ORG = os.environ.get('MORGHDARI_ORG', '359273d4-1c84-42dc-8bdc-fb13512975bd')

INFRA_SCRIPT = r'''
set -u
echo "CONTAINERS_BAD=$(docker ps --format '{{.Names}} {{.Status}}' | grep -E '^morghdari-(daphne|celery-worker|celery-beat|subscriber-01|subscriber-02|nginx|emqx|redis|postgres) ' | grep -vc '(healthy)')"
echo "HEALTH=$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/v1/health/)"
echo "BEAT_30M=$(docker logs --since 30m morghdari-celery-beat 2>&1 | grep -c 'Sending due task')"
echo "WORKER_ERR_30M=$(docker logs --since 30m morghdari-celery-worker 2>&1 | grep -ci 'traceback' || true)"
echo "SUB1_ERR_30M=$(docker logs --since 30m morghdari-subscriber-01 2>&1 | grep -ci 'traceback' || true)"
echo "SUB2_ERR_30M=$(docker logs --since 30m morghdari-subscriber-02 2>&1 | grep -ci 'traceback' || true)"
echo "TELEMETRY_AGE_MIN=$(docker exec morghdari-postgres psql -U morghdari_admin -d morghdari -t -A -c "select coalesce(round(extract(epoch from (now() - max(device_ts)))/60), -1) from saloon_telemetry;")"
echo "TELEMETRY_1H=$(docker exec morghdari-postgres psql -U morghdari_admin -d morghdari -t -A -c "select count(*) from saloon_telemetry where device_ts > now() - interval '1 hour';")"
echo "DLQ_KEYS=$(docker exec morghdari-redis redis-cli -a "$(grep -E '^REDIS_PASSWORD=' /opt/morghdari/.env | cut -d= -f2-)" --no-auth-warning --scan --pattern '*dlq*' | wc -l)"
echo "DISK_PCT=$(df --output=pcent / | tail -1 | tr -dc '0-9')"
'''

FIXTURE_SCRIPT = r'''
docker exec morghdari-celery-worker python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.prod')
django.setup()
from django.contrib.auth import get_user_model
from apps.accounts.models import OrgMember
from apps.tenants.context import admin_alias
A = admin_alias(); U = get_user_model()
n = 0
for uname in ('probe-worker','probe-engineer','probe-foreign'):
    try:
        u = U.objects.using(A).get(username=uname)
        u.is_active = True; u.save(using=A)
        m = OrgMember.objects.using(A).get(user=u)
        m.is_active = True; m.save(using=A)
        n += 1
    except Exception as e:
        print('fixture', uname, 'failed:', e)
print('FIXTURES_ACTIVE=%d' % n)
"
'''

FIRE_SCRIPT = r'''
docker exec morghdari-celery-worker python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.prod')
django.setup()
from django.db import transaction
from apps.alerts.services.firing import fire_or_coalesce
from apps.tenants.context import tenant_context
import redis, os as _os
r = redis.Redis.from_url(_os.environ['REDIS_URL'], decode_responses=True)
org = os.environ.get('_CHECK_ORG')
with transaction.atomic(), tenant_context(org):
    a = fire_or_coalesce(r, org, 3, 3, 'temp', 99.0, reason='QA health-check temp')
    b = fire_or_coalesce(r, org, 3, 3, 'humidity', 99.0, reason='QA health-check humidity')
print('FIRED=%s/%s' % (a['action'], b['action']))
" 
'''

results = []


def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('PASS ' if ok else 'FAIL ') + name + (f'  [{detail}]' if detail and not ok else ''))


def ssh_run(script, timeout=180):
    import paramiko
    host = os.environ.get('MORGHDARI_SSH_HOST', '82.115.17.252')
    user = os.environ.get('MORGHDARI_SSH_USER', 'root')
    pw = os.environ.get('MORGHDARI_SSH_PASS', '')
    if not pw:
        return -1, '', 'MORGHDARI_SSH_PASS not set'
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # look_for_keys/allow_agent OFF: password auth only — otherwise paramiko
    # probes ~/.ssh and some sandboxes block that read (noise, not a failure).
    c.connect(host, port=22, username=user, password=pw, timeout=30,
              banner_timeout=30, auth_timeout=30,
              look_for_keys=False, allow_agent=False)
    try:
        _in, out, err = c.exec_command(script, timeout=timeout)
        o = out.read().decode('utf-8', 'replace')
        e = err.read().decode('utf-8', 'replace')
        return out.channel.recv_exit_status(), o, e
    finally:
        c.close()


def kv(text):
    d = {}
    for line in text.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            d[k.strip()] = v.strip()
    return d


def main():
    skip_harness = '--skip-harness' in sys.argv

    # ── 1. INFRA ─────────────────────────────────────────────────────────────
    rc, out, err = ssh_run(INFRA_SCRIPT)
    if rc != 0:
        check('INFRA reachable', False, (err or out)[:160])
        return finish()
    m = kv(out)
    check('containers all healthy', m.get('CONTAINERS_BAD') == '0', f"bad={m.get('CONTAINERS_BAD')}")
    check('API health 200', m.get('HEALTH') == '200', f"health={m.get('HEALTH')}")
    check('celery beat dispatching', int(m.get('BEAT_30M') or 0) > 100, f"n={m.get('BEAT_30M')}")
    check('worker tracebacks 30m == 0', m.get('WORKER_ERR_30M') == '0', f"n={m.get('WORKER_ERR_30M')}")
    check('subscriber-01 tracebacks 30m == 0', m.get('SUB1_ERR_30M') == '0', f"n={m.get('SUB1_ERR_30M')}")
    check('subscriber-02 tracebacks 30m == 0', m.get('SUB2_ERR_30M') == '0', f"n={m.get('SUB2_ERR_30M')}")
    age = float(m.get('TELEMETRY_AGE_MIN') or -1)
    check('telemetry fresh (< 10 min)', 0 <= age < 10, f"age_min={age}")
    check('telemetry volume (>= 20 rows/h)', int(m.get('TELEMETRY_1H') or 0) >= 20, f"n={m.get('TELEMETRY_1H')}")
    check('DLQ keys == 0', m.get('DLQ_KEYS') == '0', f"n={m.get('DLQ_KEYS')}")
    check('disk < 85%', int(m.get('DISK_PCT') or 100) < 85, f"pct={m.get('DISK_PCT')}")

    # ── 2. FIXTURES ──────────────────────────────────────────────────────────
    rc, out, err = ssh_run(FIXTURE_SCRIPT)
    fx = kv(out)
    check('QA fixtures active (3)', fx.get('FIXTURES_ACTIVE') == '3', f"out={out[-120:]}{err[-80:]}")

    # ── 3. ALERTS (immediately before the drill — recovery sweep races) ──────
    rc, out, err = ssh_run(FIRE_SCRIPT.replace('os.environ.get(\'_CHECK_ORG\')', repr(ORG)))
    check('test alerts fired', 'FIRED=fired' in out, f"out={out[-100:]}")

    # ── 4. DRILL ─────────────────────────────────────────────────────────────
    drill = os.path.join(HARNESS_DIR, 'role_probe_drill.py')
    r = subprocess.run([sys.executable, drill], capture_output=True, text=True, timeout=600)
    line = next((l for l in r.stdout.splitlines() if 'DRILL SUMMARY' in l), '')
    m2 = re.search(r'(\d+) passed, (\d+) failed', line)
    if m2:
        p, f = int(m2.group(1)), int(m2.group(2))
        check(f'role drill green ({p} passed)', f == 0, line.strip())
    else:
        check('role drill ran', False, (r.stdout[-200:] + r.stderr[-200:]))

    # ── 5. JOURNEY (farm → device → real MQTT → telemetry) ──────────────────
    journey = os.path.join(HARNESS_DIR, 'journey_e2e.py')
    r = subprocess.run([sys.executable, journey], capture_output=True, text=True, timeout=600)
    line = next((l for l in r.stdout.splitlines() if 'JOURNEY SUMMARY' in l), '')
    m4 = re.search(r'(\d+) passed, (\d+) failed', line)
    if m4:
        p, f = int(m4.group(1)), int(m4.group(2))
        check(f'journey green ({p} steps, farm+device+MQTT)', f == 0, line.strip())
    else:
        check('journey ran', False, (r.stdout[-200:] + r.stderr[-200:]))

    # ── 6. HARNESS ───────────────────────────────────────────────────────────
    if not skip_harness:
        harness = os.path.join(HARNESS_DIR, 'api_actions.py')
        r = subprocess.run([sys.executable, harness], capture_output=True, text=True, timeout=900)
        line = next((l for l in r.stdout.splitlines() if 'SUMMARY' in l), '')
        m3 = re.search(r'(\d+) passed, (\d+) failed', line)
        if m3:
            p, f = int(m3.group(1)), int(m3.group(2))
            check(f'API harness green ({p} passed)', f == 0, line.strip())
        else:
            check('API harness ran', False, (r.stdout[-200:] + r.stderr[-200:]))

    return finish()


def finish():
    fails = [x for x in results if not x[1]]
    print(f"\n=== HEALTH CHECK: {len(results) - len(fails)} passed, {len(fails)} failed ===")
    for name, _, detail in fails:
        print('FAILED:', name, detail)
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())
