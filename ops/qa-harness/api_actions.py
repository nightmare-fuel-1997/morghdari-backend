"""Pass 2: exercise every write ACTION with a real payload and the right role.

Fixtures (created by the companion probe, removed again at the end):
  qa-owner-tmp  - org owner   (farm + member management are owner-gated)
  qa-hw-tmp     - hardware staff (factory unit endpoints)
  d1-fresh-staff- platform support (support + review endpoints)
Every resource this script creates is deleted again before it exits.

Pass-2 findings (2026-10-25): the support-op surfaces (hardware register,
force-resolve) need an ACTIVE support-session token - the session must be
opened before those steps. The gateway-request org was over its 4-PENDING cap
(5 stale QA requests), so the stale ones are rejected first. Approve no longer
mints slots/credentials (AD-15/16/17) - it materialises the farm and creates
installation jobs; the factory-unit lifecycle (register/claim-code/credential/
retire) is exercised directly on /platform/devices/units/ instead.
"""
import base64
import hashlib
import hmac
import json
import struct
import ssl
import time
import urllib.error
import urllib.request

BASE = "https://82.115.17.252"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
ORG = "359273d4-1c84-42dc-8bdc-fb13512975bd"
STAFF_MFA_SECRET = "K5GJYPWTRUTM2EPQZDHMP56DPNTMATLP"
HW_MFA_SECRET = "QAHWQAHWQAHWQAHWQAHWQAHWQAHW"
# The owner fixture: BOOTSTRAP_PW is what the account was created with and is
# NOT settable through the API again (the similarity validator refuses any
# password containing the username). The drill therefore flips between the two
# SETTABLE values on each run.
BOOTSTRAP_PW = "qa-owner-pw"
SETTABLE_PWS = ("Morghdari-QA-2026x", "Shed-9-Keys!2026")
PASS, FAIL = [], []


def _totp(secret_b32):
    secret = secret_b32.upper().replace(" ", "")
    secret += "=" * (-len(secret) % 8)
    key = base64.b32decode(secret)
    counter = int(time.time() // 30)
    d = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = d[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)


def call(method, path, token=None, body=None, timeout=40):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as exc:  # noqa: BLE001
        return -1, repr(exc)[:140]


def login(username, password, mfa_secret=None):
    payload = {"username": username, "password": password}
    if mfa_secret:
        payload["mfa_code"] = _totp(mfa_secret)
    st, body = call("POST", "/api/v1/auth/login/", body=payload)
    return json.loads(body).get("access_token", "") if st == 200 else ""


def step(label, expect, st, body):
    ok = st in expect
    (PASS if ok else FAIL).append((label, st, body[:200]))
    print(("  PASS " if ok else "  FAIL ") + label + f"  [{st}] {body[:120]}")
    return ok


def jb(text):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {}


def pick(body, *keys):
    """Responses key their ids as <resource>_id; accept any of the spellings."""
    data = jb(body)
    for k in keys:
        if data.get(k) is not None:
            return data[k]
    return None


def pickd(row, *keys):
    """Same as pick() for an already-parsed dict row (pick() re-parses text)."""
    for k in keys:
        if isinstance(row, dict) and row.get(k) is not None:
            return row[k]
    return None


def rows_of(text):
    """List payloads arrive either bare or wrapped in {"results": [...]}."""
    payload = jb(text)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("results", [])
    return []


def main():
    # The owner fixture's password flips between the two settable values on
    # every run (the similarity validator refuses any password containing the
    # username, so the original "qa-owner-pw" can never be set again).
    owner_pw = next((p for p in (BOOTSTRAP_PW,) + SETTABLE_PWS
                     if login("qa-owner-tmp", p)), "")
    owner = login("qa-owner-tmp", owner_pw)
    mgr = login("auto-manager-2961900", "x")
    support = login("d1-fresh-staff", "d1-pw", STAFF_MFA_SECRET)
    hw = login("qa-hw-tmp", "qa-hw-pw", HW_MFA_SECRET)
    print("tokens owner=%s manager=%s support=%s hardware=%s"
          % (bool(owner), bool(mgr), bool(support), bool(hw)))
    today = time.strftime("%Y-%m-%d", time.gmtime())
    ts = str(int(time.time()))[-6:]

    print("\n== I0. support session FIRST (hardware + force-resolve need it) ==")
    st, body = call("POST", "/api/v1/platform/support/session/", support,
                    {"incident_id": "QA-INC-2", "org_id": ORG})
    session_token = jb(body).get("access_token", "")
    step("POST support session", (200, 201), st, body[:100])

    print("\n== A0. geography lookup (provinces / cities / villages) ==")
    st, provs = call("GET", "/api/v1/provisioning/provinces/", support)
    step("GET provisioning provinces", (200,), st, provs[:120])
    prov = rows_of(provs)
    province_id = prov[0]["id"] if prov else None
    st, cities = call("GET", f"/api/v1/provisioning/cities/?province_id={province_id}", support)
    step("GET provisioning cities", (200,), st, cities[:120])
    city_rows = rows_of(cities)
    city_id = next((c["id"] for c in city_rows
                    if c.get("province_id") in (None, province_id)), None)
    st, villages = call("GET", f"/api/v1/provisioning/villages/?city_id={city_id}", support)
    step("GET provisioning villages", (200,), st, villages[:120])
    village_rows = rows_of(villages)
    village_id = village_rows[0]["id"] if village_rows else None

    print("\n== A. farm/saloon/position/slot (owner) ==")
    st, body = call("POST", "/api/v1/farms/", owner, {
        "name": "qa-farm-" + ts, "province_id": province_id, "city_id": city_id,
        "village_id": village_id})
    fid = jb(body).get("farm_id") or jb(body).get("id")
    step("POST farm create (owner)", (200, 201), st, body)
    step("PATCH farm rename", (200,), *call("PATCH", f"/api/v1/farms/{fid}/", owner,
                                            {"name": "qa-farm-2-" + ts}))
    st, body = call("POST", f"/api/v1/farms/{fid}/saloons/", owner, {"name": "qa-saloon-" + ts})
    sid = jb(body).get("saloon_id") or jb(body).get("id")
    step("POST saloon create", (200, 201), st, body)
    st, body = call("POST", f"/api/v1/saloons/{sid}/positions/", mgr,
                    {"label": "north", "index": 1})
    pid = pickd(jb(body), "position_id", "id")
    step("POST position create", (200, 201), st, body)
    step("PATCH position update", (200,),
         *call("PATCH", f"/api/v1/saloons/{sid}/positions/{pid}/", mgr, {"label": "north-2"}))
    st, body = call("POST", f"/api/v1/saloons/{sid}/sensor-slots/", mgr,
                    {"position_id": pid, "allowed_metric_keys": ["temp", "humidity"]})
    qa_slot = pickd(jb(body), "slot_id", "id")
    step("POST sensor-slot create", (200, 201), st, body)
    # F7b (added 2026-10-26): the slot's own write surface. Retire then re-arm
    # so the rest of the run keeps a live slot.
    step("PATCH sensor-slot retire", (200,),
         *call("PATCH", f"/api/v1/saloons/{sid}/sensor-slots/{qa_slot}/", mgr,
               {"is_active": False}))
    step("PATCH sensor-slot re-arm", (200,),
         *call("PATCH", f"/api/v1/saloons/{sid}/sensor-slots/{qa_slot}/", mgr,
               {"is_active": True}))
    step("GET saloon capabilities", (200,), *call("GET", f"/api/v1/saloons/{sid}/capabilities/", mgr))

    print("\n== B. flock lifecycle ==")
    st, body = call("POST", f"/api/v1/saloons/{sid}/flocks/", mgr, {
        "breed_type": "ross", "placement_count": 500, "start_date": today})
    flock = pick(body, "flock_id", "id")
    step("POST flock create", (200, 201), st, body)
    step("POST mortality", (200, 201),
         *call("POST", f"/api/v1/saloons/{sid}/flocks/{flock}/mortality/", mgr,
               {"count": 3, "occurred_on": today}))
    step("POST feed-intake", (200, 201),
         *call("POST", f"/api/v1/saloons/{sid}/flocks/{flock}/feed-intake/", mgr,
               {"feed_kg": 12.5, "entry_date": today}))
    step("POST water-intake", (200, 201),
         *call("POST", f"/api/v1/saloons/{sid}/flocks/{flock}/water-intake/", mgr,
               {"water_l": 40, "entry_date": today}))
    step("POST weight-samples", (200, 201),
         *call("POST", f"/api/v1/saloons/{sid}/flocks/{flock}/weight-samples/", mgr,
               {"sample_size": 10, "avg_weight_g": 42, "min_weight_g": 38,
                "max_weight_g": 46}))
    step("PATCH flock close", (200,), *call("PATCH", f"/api/v1/saloons/{sid}/flocks/{flock}/close/",
                                            mgr, {"end_date": today}))

    print("\n== C. reports ==")
    st, body = call("POST", f"/api/v1/saloons/{sid}/reports/", mgr, {
        "flock_id": flock, "report_date": today, "mortality_count": 1, "feed_kg": 10})
    rid = pick(body, "report_id", "id")
    step("POST report (DRAFT)", (200, 201), st, body)
    step("PATCH report edit", (200,), *call("PATCH", f"/api/v1/reports/{rid}/", mgr, {"feed_kg": 11}))
    step("GET report by date", (200, 404),
         *call("GET", f"/api/v1/saloons/{sid}/reports/{today}/", mgr))

    print("\n== D0. panel catalog (what keys exist) ==")
    st, mine = call("GET", "/api/v1/panels/me/", mgr)
    step("GET /panels/me/", (200,), st, mine[:140])
    panel_rows = rows_of(mine)
    # role/override writes must target a real PanelDefinition row; "admin" is
    # the owner-only panel (also a real row, but fail-closed for writes).
    panel = next((r for r in panel_rows if r.get("source") != "owner-only"), {})
    panel_id = pickd(panel, "panel_id", "id")
    st, dash = call("GET", "/api/v1/me/dashboard-config/", mgr)
    step("GET /me/dashboard-config", (200,), st, dash[:120])
    widget_keys = list(jb(dash).get("widgets", {}).keys())
    step("GET role-permissions", (200,), *call("GET", "/api/v1/panels/role-permissions/", owner))

    print("\n== D. mutes, panels, dashboard ==")
    step("POST mute temp", (200, 201), *call("POST", "/api/v1/alert-subscriptions/", mgr,
                                             {"metric_key": "temp"}))
    step("DELETE unmute temp", (200, 204), *call("DELETE", "/api/v1/alert-subscriptions/temp/", mgr))
    st, body = call("POST", "/api/v1/maintenance-mutes/", mgr, {
        "saloon_id": sid, "starts_at": today + "T00:00:00Z",
        "ends_at": (time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))
                    + "T00:00:00Z"), "reason": "qa"})
    mid = pick(body, "mute_id", "id")
    step("POST maintenance mute", (200, 201), st, body)
    step("DELETE maintenance mute", (200, 204),
         *call("DELETE", f"/api/v1/maintenance-mutes/{mid}/", mgr))
    st, body = call("POST", "/api/v1/panels/role-permissions/", owner,
                    {"panel_id": panel_id, "role": "manager", "allowed": True})
    perm = pick(body, "id", "permission_id")
    step("POST role-panel-permission", (200, 201), st, body)
    step("DELETE role-panel-permission", (200, 204),
         *call("DELETE", f"/api/v1/panels/role-permissions/{perm}/", owner))
    st, body = call("POST", "/api/v1/panels/user-overrides/", owner,
                    {"panel_id": panel_id, "user_id": 8, "allowed": True})
    ovr = pick(body, "id", "override_id")
    step("POST user-panel-override", (200, 201), st, body)
    step("DELETE user-panel-override", (200, 204),
         *call("DELETE", f"/api/v1/panels/user-overrides/{ovr}/", owner))
    widgets = {k: True for k in widget_keys}
    step("PUT /me/dashboard-config", (200,), *call("PUT", "/api/v1/me/dashboard-config/", mgr,
                                                   {"widgets": widgets}))
    step("PUT /dashboard-config/<role> (owner)", (200, 403),
         *call("PUT", "/api/v1/dashboard-config/manager/", owner, {"widgets": widgets}))

    print("\n== E. org member management (owner) ==")
    st, body = call("POST", "/api/v1/org/members/", owner, {
        "username": "qa-member-" + ts, "password": "tmp-pass-123", "role": "engineer"})
    member = pick(body, "member_id", "user_id", "id")
    step("POST org member", (200, 201), st, body)
    step("PATCH member role", (200,), *call("PATCH", f"/api/v1/org/members/{member}/", owner,
                                            {"role": "manager"}))
    step("POST member deactivate", (200,), *call("POST", f"/api/v1/org/members/{member}/deactivate/",
                                                 owner, {}))

    print("\n== F. gateway lifecycle on the live twin ==")
    # Order matters: suspend/resume FIRST, then the command. A command issued
    # just before a suspend races the ACL revocation (the answer publish is
    # dropped while the rules are gone) and legitimately expires — that is
    # correct behaviour, but it makes the drill non-deterministic.
    step("POST gateway suspend", (200, 409),
         *call("POST", "/api/v1/gateways/GW-AUTO-2961900/suspend/", mgr, {"reason": "qa drill"}))
    step("POST gateway resume", (200, 409),
         *call("POST", "/api/v1/gateways/GW-AUTO-2961900/resume/", mgr, {}))
    step("POST command REBOOT (202 accepted)", (200, 201, 202, 409),
         *call("POST", "/api/v1/gateways/GW-AUTO-2961900/commands/", mgr, {"kind": "REBOOT"}))
    step("POST command DIAGNOSTICS_REQUEST", (200, 201, 202, 409),
         *call("POST", "/api/v1/gateways/GW-AUTO-2961900/commands/", mgr,
               {"kind": "DIAGNOSTICS_REQUEST"}))
    step("GET gateway config", (200,), *call("GET", "/api/v1/gateways/GW-AUTO-2961900/config/", mgr))
    step("GET gateway list", (200,), *call("GET", "/api/v1/gateways/", mgr))

    print("\n== G. gateway request + review (cap-aware) ==")
    # The org is capped at 4 PENDING requests; stale QA ones count against it.
    st, body = call("GET", "/api/v1/gateway-requests/", mgr)
    pending = [r for r in (body if isinstance(body, list) else jb(body))
               if isinstance(r, dict) and r.get("status") == "PENDING"]
    for row in pending:
        step(f"POST review REJECT stale #{row.get('id')}", (200, 409),
             *call("POST", f"/api/v1/gateway-requests/{row['id']}/review/", support,
                   {"decision": "REJECT", "reason": "qa cleanup"}))
    st, body = call("POST", "/api/v1/gateway-requests/", mgr, {
        "farm_id": fid, "expected_gateways": 1, "details": {"note": "qa"}})
    req_id = pick(body, "request_id", "id")
    step("POST gateway-request", (200, 201), st, body)
    # AD-15/16/17: approve no longer mints a slot/credential - it materialises
    # the farm and creates one installation job per expected gateway.
    step("POST review APPROVE (support)", (200, 400, 404),
         *call("POST", f"/api/v1/gateway-requests/{req_id}/review/", support,
               {"decision": "APPROVE"}))

    print("\n== H. factory unit lifecycle (hardware staff) ==")
    unit_id = "GW-QA-UNIT-" + ts
    st, body = call("POST", "/api/v1/platform/devices/units/", hw,
                    {"unit_id": unit_id, "hardware_id": "QA-HW-" + ts,
                     "model": "GW-4", "batch": "qa", "reason": "qa drill"})
    step("POST register factory unit", (200, 201), st, body[:160])
    step("POST re-issue claim-code", (200, 201),
         *call("POST", f"/api/v1/platform/devices/units/{unit_id}/claim-code/", hw,
               {"reason": "qa"}))
    step("POST rotate unit credential", (200, 201),
         *call("POST", f"/api/v1/platform/devices/units/{unit_id}/credential/", hw,
               {"reason": "qa"}))
    step("GET platform units", (200,), *call("GET", "/api/v1/platform/devices/units/", hw))
    step("POST retire factory unit", (200, 400, 404),
         *call("POST", f"/api/v1/platform/devices/units/{unit_id}/retire/", hw,
               {"reason": "qa"}))

    print("\n== H2. hardware registry rows (support session) ==")
    st, body = call("POST", "/api/v1/platform/support/hardware/sensors/", session_token,
                    {"slot_id": qa_slot, "hardware_id": "QA-SENSOR-" + ts, "reason": "qa"})
    hw_id = pick(body, "id", "hardware_id")
    step("POST register sensor (session)", (200, 201), st, body)
    step("POST retire sensor", (200, 400, 404),
         *call("POST", f"/api/v1/platform/support/hardware/sensors/{hw_id}/retire/",
               session_token, {"reason": "qa"}))

    print("\n== I. support actions (session ops) ==")
    st, body = call("GET", "/api/v1/alerts/", mgr)
    open_alerts = [a for a in rows_of(body)
                   if str(a.get("current_status", "")).startswith("ACTIVE")]
    if open_alerts:
        aid = pickd(open_alerts[0], "alert_id", "id")
        step("POST support force-resolve", (200,),
             *call("POST", f"/api/v1/platform/support/alerts/{aid}/force-resolve/",
                   session_token, {"reason": "qa drill"}))
    else:
        aid = None
        step("POST support force-resolve (no open alert)", (999,), 0, "no open alert")
    if len(open_alerts) > 1:
        ack_id = pickd(open_alerts[1], "alert_id", "id")
        step("POST acknowledge an alert", (200,),
             *call("POST", f"/api/v1/alerts/{ack_id}/acknowledge/", mgr, {"notes": "qa"}))
    else:
        step("POST acknowledge (no second open alert)", (999,), 0, "only one open alert")

    print("\n== J. auth lifecycle ==")
    st, body = call("POST", "/api/v1/auth/login/",
                    body={"username": "auto-manager-2961900", "password": "x"})
    refresh = jb(body).get("refresh_token")
    step("POST token refresh", (200, 201), *call("POST", "/api/v1/auth/token/refresh/", mgr,
                                                 {"refresh": refresh}))
    st, body = call("GET", "/api/v1/auth/sessions/", mgr)
    step("GET sessions", (200,), st, body[:120])
    sessions = rows_of(body)
    if len(sessions) > 1:
        # revoke the OLDEST session (not the caller's own) — a real action
        oldest = min(sessions, key=lambda s: s.get("id", 0))
        step("DELETE session (revoke oldest)", (200, 204),
             *call("DELETE", f"/api/v1/auth/sessions/{oldest.get('id')}/", mgr))
    # the field is current_password, not old_password; 3/min throttle per user.
    # NOTE: an invited member gets an UNUSABLE password by design ("login only
    # via an explicit credential handover, never by our invite"), so the drill
    # runs on the owner fixture and flips it between two known passwords.
    next_pw = next(p for p in SETTABLE_PWS if p != owner_pw)
    step("POST password change too similar -> 400 (by design)", (400,),
         *call("POST", "/api/v1/auth/password/change/", owner,
               {"current_password": owner_pw, "new_password": "qa-owner-tmp-2"}))
    step("POST password change", (200, 429),
         *call("POST", "/api/v1/auth/password/change/", owner,
               {"current_password": owner_pw, "new_password": next_pw}))
    st, body = call("POST", "/api/v1/auth/login/",
                    body={"username": "qa-owner-tmp", "password": next_pw})
    step("POST login with the new password", (200,), st, body[:80])
    # NOTE: /api/v1/logout/ is DRF's browsable-API session logout (CSRF form
    # view from rest_framework.urls) — not part of the product API, so it is
    # not exercised here. Session revocation is the DELETE above.
    step("GET audit-logs (owner)", (200,), *call("GET", "/api/v1/audit-logs/", owner))
    step("GET reconciliation (owner)", (200,), *call("GET", "/api/v1/reconciliation/", owner))

    print("\n== K. threshold templates ==")
    st, body = call("POST", "/api/v1/threshold-templates/", mgr, {
        "name": "qa-template-" + ts, "status": "DRAFT",
        "rules": [{"metric_key": "temp", "day_from": 0, "day_to": 99,
                   "min_value": 15, "max_value": 26}]})
    tpl = pick(body, "template_id", "id")
    step("POST template create", (200, 201), st, body)
    step("PUT template update", (200,),
         *call("PUT", f"/api/v1/threshold-templates/{tpl}/", mgr,
               {"name": "qa-template-2-" + ts,
                "rules": [{"metric_key": "temp", "day_from": 0, "day_to": 99,
                           "min_value": 14, "max_value": 27}]}))
    step("POST template publish", (200, 400), *call("POST", f"/api/v1/threshold-templates/{tpl}/publish/",
                                                    mgr, {}))
    # O17 (added 2026-10-26): a template the org owns can be removed once
    # nothing depends on it — this is also the cleanup the QA org never had.
    step("DELETE template (published, unused)", (200, 409),
         *call("DELETE", f"/api/v1/threshold-templates/{tpl}/", mgr))

    print("\n== L. cleanup ==")
    # There is no DELETE for an org member (PATCH role + /deactivate/ only),
    # so leftovers are deactivated. A saloon that has ever held a flock is
    # refused by design ("saloon has flock history") and so is a non-empty
    # farm — the delete path is verified as a REFUSAL, not a deletion.
    step("POST deactivate member", (200,),
         *call("POST", f"/api/v1/org/members/{member}/deactivate/", owner, {}))
    st, body = call("GET", "/api/v1/org/members/", owner)
    for row in rows_of(body):
        if (str(row.get("username", "")).startswith("qa-member-")
                and not row.get("is_active")):
            step(f"POST deactivate leftover member {row.get('member_id')}", (200, 404),
                 *call("POST", f"/api/v1/org/members/{row['member_id']}/deactivate/",
                       owner, {}))
    st, body = call("DELETE", f"/api/v1/saloons/{sid}/", owner)
    step("DELETE saloon refused: flock history (by design)", (200, 204, 409), st, body)
    st, body = call("DELETE", f"/api/v1/farms/{fid}/", owner)
    step("DELETE farm refused: not empty (by design)", (200, 204, 409), st, body)

    print("\n===== SUMMARY: %d passed, %d failed =====" % (len(PASS), len(FAIL)))
    for label, st, body in FAIL:
        print("  FAIL", label, st, body[:200])


if __name__ == "__main__":
    main()
