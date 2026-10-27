"""End-to-end journey: farm lifecycle + device provisioning + real MQTT telemetry.

This is the deepest chain test in the suite. It exercises EVERY actor in
sequence and ends with a real MQTT client publishing on the tenant lane —
the full device → broker → subscriber → Celery → DB → API path that no other
test covers (the live suite elsewhere calls reconcilers directly).

Chain A — farm lifecycle (owner):
    provinces → cities → villages → farm → saloon → position → sensor slot
    → threshold template (create/publish/assign) → flock → operational state
    → reads (overview, saloon, KPIs, capabilities)

Chain B — device provisioning (manager → support → hardware staff → device):
    gateway request → support APPROVE (materialises farm + install job)
    → factory unit register (hardware) → claim code → credential
    → installer bind (unit + claim code) → sensor mapping → submit
    → REAL MQTT: connect → retained desired → APPLIED report → PROVISIONED
    → status ONLINE (retained + LWT) → telemetry schema 2.0
    → DB row visible through the REST API

Usage:
    python ops/qa-harness/journey_e2e.py

Env:
    MORGHDARI_BASE   default https://82.115.17.252
"""
import base64
import hashlib
import hmac
import json
import os
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get('MORGHDARI_BASE', 'https://82.115.17.252')
HOST = os.environ.get('MORGHDARI_MQTT_HOST', '82.115.17.252')
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

ORG = os.environ.get('MORGHDARI_ORG', '359273d4-1c84-42dc-8bdc-fb13512975bd')
STAFF_MFA = 'K5GJYPWTRUTM2EPQZDHMP56DPNTMATLP'
HW_MFA = 'QAHWQAHWQAHWQAHWQAHWQAHWQAHW'
OWNER_CANDIDATES = [p for p in (
    os.environ.get('MORGHDARI_OWNER_PW', ''),
    'qa-owner-pw', 'Morghdari-QA-2026x', 'Shed-9-Keys!2026',
) if p]

PASS, FAIL = [], []


def step(name, ok, detail=''):
    (PASS if ok else FAIL).append((name, detail))
    print(('PASS ' if ok else 'FAIL ') + name + (f'  [{detail}]' if detail and not ok else ''))


def totp(secret):
    key = base64.b32decode(secret.upper() + '=' * (-len(secret) % 8))
    counter = int(time.time() // 30)
    d = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    o = d[-1] & 0x0F
    return '%06d' % ((struct.unpack('>I', d[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)


def call(method, path, token=None, body=None):
    h = {}
    d = None
    if token:
        h['Authorization'] = 'Bearer ' + token
    if body is not None:
        d = json.dumps(body).encode()
        h['Content-Type'] = 'application/json'
    # Transport-level retry: this drill fires 25+ requests back-to-back, each a
    # fresh TLS handshake; the odd SSL EOF / reset here is network flake, not a
    # product signal. Retrying is safe (the journey is idempotent by design).
    last = None
    for attempt in range(4):
        req = urllib.request.Request(BASE + path, data=d, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except Exception as e:
            last = repr(e)[:140]
            time.sleep(1.5 * (attempt + 1))
    return -1, last


def jb(text):
    try:
        return json.loads(text)
    except Exception:
        return {}


def rows(text):
    p = jb(text)
    return p if isinstance(p, list) else p.get('results', [])


def login(username, password, mfa=None):
    body = {'username': username, 'password': password}
    if mfa:
        body['mfa_code'] = totp(mfa)
    st, b = call('POST', '/api/v1/auth/login/', body=body)
    return jb(b).get('access_token', '') if st == 200 else ''


def main():
    ts = str(int(time.time()))
    print(f'== journey {ts} ==')

    # ── actors ────────────────────────────────────────────────────────────────
    owner = next((t for t in (login('qa-owner-tmp', p) for p in OWNER_CANDIDATES) if t), '')
    mgr = login('auto-manager-2961900', 'x')
    support = login('d1-fresh-staff', 'd1-pw', STAFF_MFA)
    hw = login('qa-hw-tmp', 'qa-hw-pw', HW_MFA)
    step('actor logins (owner/manager/support/hardware)', all([owner, mgr, support, hw]),
         f'owner={bool(owner)} mgr={bool(mgr)} support={bool(support)} hw={bool(hw)}')

    # ── CHAIN A: farm lifecycle ──────────────────────────────────────────────
    # Geography is platform-staff scoped (support token) and keyed on `id`.
    st, b = call('GET', '/api/v1/provisioning/provinces/', support)
    prov = rows(b)
    pid = prov[0].get('id') if prov else None
    st, b = call('GET', f'/api/v1/provisioning/cities/?province_id={pid}', support)
    cities = rows(b)
    cid = next((c.get('id') for c in cities if c.get('province_id') in (None, pid)), None)
    vid = None
    for c in cities[:5]:
        st, b = call('GET', f"/api/v1/provisioning/villages/?city_id={c.get('id')}", support)
        v = rows(b)
        if v:
            cid, vid = c.get('id'), v[0].get('id')
            break
    # An empty village catalog is data, not a defect: farm create accepts a
    # village-less (city-level) location, so only province+city are required.
    step('A1 geography chain (province→city→village)',
         bool(pid) and bool(cid), f'{pid}/{cid}/{vid} (village optional)')

    st, b = call('POST', '/api/v1/farms/', owner,
                 {'name': 'journey-farm-' + ts, 'province_id': pid, 'city_id': cid, 'village_id': vid})
    fid = jb(b).get('farm_id') or jb(b).get('id')
    step('A2 farm create (owner)', st in (200, 201) and bool(fid), f'st={st} {b[:120]}')

    st, _ = call('PATCH', f'/api/v1/farms/{fid}/', owner, {'name': 'journey-farm2-' + ts})
    step('A3 farm rename (owner)', st == 200, f'st={st}')

    st, b = call('POST', f'/api/v1/farms/{fid}/saloons/', owner, {'name': 'journey-saloon-' + ts})
    sid = jb(b).get('saloon_id') or jb(b).get('id')
    step('A4 saloon create (owner)', st in (200, 201) and bool(sid), f'st={st} {b[:120]}')

    st, b = call('POST', f'/api/v1/saloons/{sid}/positions/', owner, {'label': 'journey-pos-' + ts})
    posid = jb(b).get('position_id')
    step('A5 position create', st in (200, 201) and bool(posid), f'st={st} {b[:120]}')

    st, b = call('POST', f'/api/v1/saloons/{sid}/sensor-slots/', owner,
                 {'position_id': posid, 'allowed_metric_keys': ['temp', 'humidity', 'nh3']})
    slot = jb(b).get('slot_id')
    step('A6 sensor slot create', st in (200, 201) and bool(slot), f'st={st} {b[:120]}')

    rules = [{'metric_key': 'temp', 'day_from': 0, 'day_to': 99, 'min_value': 15, 'max_value': 26}]
    st, b = call('POST', '/api/v1/threshold-templates/', owner,
                 {'name': 'journey-tpl-' + ts, 'status': 'DRAFT', 'rules': rules})
    tpl = jb(b).get('template_id')
    st2, _ = call('POST', f'/api/v1/threshold-templates/{tpl}/publish/', owner, {'is_default': False})
    st3, _ = call('PUT', f'/api/v1/saloons/{sid}/threshold-template/', owner, {'template_id': tpl})
    step('A7 template create→publish→assign', st == 201 and st2 == 200 and st3 == 200,
         f'{st}/{st2}/{st3}')

    st, b = call('POST', f'/api/v1/saloons/{sid}/flocks/', owner,
                 {'breed_type': 'ross', 'placement_count': 500, 'start_date': time.strftime('%Y-%m-%d')})
    flock = jb(b).get('flock_id') or jb(b).get('id')
    step('A8 flock placement', st in (200, 201) and bool(flock), f'st={st} {b[:120]}')

    st, b = call('POST', f'/api/v1/saloons/{sid}/operational-state/', owner, {'state': 'PRE_HEATING'})
    step('A9 operational state transition', st in (200, 201), f'st={st} {b[:100]}')

    st, _ = call('GET', f'/api/v1/farms/{fid}/overview/', owner)
    st2, _ = call('GET', f'/api/v1/saloons/{sid}/', owner)
    st3, _ = call('GET', f'/api/v1/saloons/{sid}/kpis/', owner)
    st4, _ = call('GET', f'/api/v1/saloons/{sid}/capabilities/', owner)
    step('A10 reads (overview/saloon/kpis/capabilities)', all(x == 200 for x in (st, st2, st3, st4)),
         f'{st}/{st2}/{st3}/{st4}')

    # ── CHAIN B: device provisioning ─────────────────────────────────────────
    st, b = call('POST', '/api/v1/gateway-requests/', mgr,
                 {'farm_id': fid, 'expected_gateways': 1, 'details': {'note': 'journey'}})
    req = jb(b).get('request_id') or jb(b).get('id')
    step('B1 gateway request (manager)', st in (200, 201) and bool(req), f'st={st} {b[:120]}')

    st, b = call('POST', f'/api/v1/gateway-requests/{req}/review/', support, {'decision': 'APPROVE'})
    step('B2 support APPROVE (materialises install job)', st in (200, 201), f'st={st} {b[:140]}')

    unit = 'GW-JRN-' + ts
    st, b = call('POST', '/api/v1/platform/devices/units/', hw,
                 {'unit_id': unit, 'hardware_id': 'JRN-HW-' + ts, 'model': 'GW-4',
                  'batch': 'journey', 'reason': 'journey drill'})
    claim = jb(b).get('claim_code', '')
    step('B3 factory unit register (hardware staff)', st in (200, 201) and bool(claim),
         f'st={st} {b[:140]}')
    if not claim:
        st, b = call('POST', f'/api/v1/platform/devices/units/{unit}/claim-code/', hw, {'reason': 'journey'})
        claim = jb(b).get('claim_code', '')

    st, b = call('POST', f'/api/v1/platform/devices/units/{unit}/credential/', hw, {'reason': 'journey'})
    mqtt_pw = jb(b).get('password', '')
    step('B4 device credential issued', st in (200, 201) and bool(mqtt_pw), f'st={st}')

    st, b = call('GET', '/api/v1/provisioning/installations/?state=PENDING', support)
    job = next((r for r in rows(b) if str(r.get('farm_id')) == str(fid)), None)
    job_id = job['id'] if job else None
    step('B5 installation job visible (support queue)', bool(job_id), f'st={st} n={len(rows(b))}')

    st, b = call('POST', f'/api/v1/provisioning/installations/{job_id}/gateway/', support,
                 {'unit_id': unit, 'claim_code': claim})
    step('B6 installer bind (unit + claim code)', st in (200, 201), f'st={st} {b[:140]}')

    pack = '%08x' % (int(time.time()) & 0xFFFFFFFF)
    st, b = call('POST', f'/api/v1/provisioning/installations/{job_id}/sensors/', support,
                 {'pack_id': pack, 'slot_id': slot})
    step('B7 sensor mapping (pack_id → slot)', st in (200, 201), f'st={st} {b[:140]}')

    st, b = call('POST', f'/api/v1/provisioning/installations/{job_id}/submit/', support)
    step('B8 installer submit (BOUND → SUBMITTED)', st in (200, 201), f'st={st} {b[:140]}')

    # ── CHAIN C: REAL MQTT — device connects, configures, reports ───────────
    mqtt_ok, telemetry_ok = mqtt_journey(unit, mqtt_pw, org=ORG, farm=fid, slot=slot, pack=pack, sid=sid, token=owner)

    # slot activation after APPLIED report (AD-17: no human approval)
    state = None
    for _ in range(10):
        st, b = call('GET', f'/api/v1/provisioning/installations/{job_id}/', support)
        view = jb(b)
        state = view.get('gateway_state_view') or view.get('state')
        if state in ('PROVISIONED', 'COMPLETED'):
            break
        time.sleep(2)
    step('C4 slot PROVISIONED after APPLIED (AD-17)', state in ('PROVISIONED', 'COMPLETED'),
         f'state={state}')

    st, b = call('GET', '/api/v1/provisioning/config-state/', support)
    step('C5 convergence view reachable', st == 200, f'st={st}')

    # ── summary ──────────────────────────────────────────────────────────────
    print(f'\n=== JOURNEY SUMMARY: {len(PASS)} passed, {len(FAIL)} failed ===')
    for name, detail in FAIL:
        print('FAILED:', name, detail)
    return 0 if not FAIL else 1


def mqtt_journey(unit, password, org, farm, slot, pack, sid, token=''):
    """The real device: paho over TLS, device lanes → tenant lanes."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        step('C0 paho available', False, 'paho-mqtt not installed')
        return False, False

    received = {'desired': None, 'reports_sent': [], 'suback': []}
    client = mqtt.Client(client_id='journey-' + unit, protocol=mqtt.MQTTv311, clean_session=True)
    client.username_pw_set(unit, password)
    client.tls_set_context(CTX)

    def on_connect(c, u, flags, rc, properties=None):
        c.subscribe(f'devices/{unit}/desired', qos=1)

    def on_message(c, u, msg):
        if msg.topic == f'devices/{unit}/desired':
            received['desired'] = msg.payload

    client.on_connect = on_connect
    client.on_message = on_message
    connected = False
    last_err = 'connect failed'
    for attempt in range(3):
        try:
            client.connect(HOST, 8883, keepalive=30)
            client.loop_start()
            # The FIRST desired document is published by a queued task after
            # bind, so a device that connects within seconds may subscribe to
            # an empty retained slot. Real firmware just stays subscribed; we
            # re-subscribe every 5 s (idempotent) until the doc arrives.
            deadline = time.time() + 60
            last_poke = 0
            while time.time() < deadline and received['desired'] is None:
                if time.time() - last_poke > 5:
                    client.subscribe(f'devices/{unit}/desired', qos=1)
                    last_poke = time.time()
                time.sleep(0.3)
            connected = True
            break
        except Exception as e:
            last_err = repr(e)[:120]
            try:
                client.loop_stop()
            except Exception:
                pass
            time.sleep(3 * (attempt + 1))
    if not connected:
        step('C1 MQTT connect + retained desired delivered', False, last_err)
        return False, False

    step('C1 MQTT connect + retained desired delivered', received['desired'] is not None)
    doc = json.loads(received['desired']) if received['desired'] else {}
    step('C2 desired doc addressed to this unit', doc.get('slot_id') == unit,
         f"slot_id={doc.get('slot_id')}")

    try:
        ver = int(doc.get('config_version') or 0)
        client.publish(f'devices/{unit}/reported',
                       json.dumps({'schema_version': '1.0', 'slot_id': unit,
                                   'config_version': ver, 'state': 'APPLIED',
                                   'error': None,
                                   'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}),
                       qos=1)
        step('C3 APPLIED report published', True, f'config_version={ver}')

        t_base = f'{org}/farms/{farm}/gateways/{unit}'
        client.subscribe(f'{t_base}/commands', qos=1)
        client.publish(f'{t_base}/status',
                       json.dumps({'gateway_slot_id': unit, 'status': 'ONLINE'}),
                       qos=1, retain=True)
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        payload = {
            'gateway_hardware_id': 'JRN-HW-' + unit[-10:],
            'gateway_slot_id': unit,
            'schema_version': '2.0',
            'device_ts': now,
            'is_buffered': False,
            'clock_drift': False,
            'readings': [{'pack_id': pack,
                          'metrics': {'temp': {'avg': 31.5, 'min': 30.9, 'max': 32.2},
                                      'humidity': {'avg': 58.0, 'min': 56.0, 'max': 60.0}}}],
        }
        client.publish(f'{t_base}/telemetry', json.dumps(payload), qos=1)
        time.sleep(4)
    except Exception as e:
        step('C3b tenant-lane publish', False, repr(e)[:140])
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    # verify the row through the REST API
    st, b = call('GET', f'/api/v1/saloons/{sid}/telemetry/?since='
                 + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 600)), token)
    got = [r for r in rows(b)
           if str(r.get('slot_id')) == str(slot) or str(r.get('gateway_slot_id')) == unit]
    step('C6 telemetry row visible via REST API', st == 200 and bool(got),
         f'st={st} rows={len(rows(b))} matched={len(got)}')
    return True, bool(got)


if __name__ == '__main__':
    sys.exit(main())
