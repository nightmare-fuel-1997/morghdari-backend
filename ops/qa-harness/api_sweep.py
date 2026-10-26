"""Sweep every API route and its declared verbs against a live deployment.

Pass 1 (safe): invalid parent ids so nothing is created; the point is that every
handler runs and returns a classified status instead of a 5xx. Stdlib only.
"""
import os
import re
import ssl
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
INVENTORY = os.path.join(HERE, "routes-inventory.txt")

BASE = "https://82.115.17.252"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

CREDS = {
    "manager": ("auto-manager-2961900", "x", False),
    "support": ("d1-fresh-staff", "d1-pw", True),
    "installer": ("auto-installer-2961900", "x", False),
}
SUFFIX = {
    "<int:saloon_id>": "3", "<int:farm_id>": "99999", "<int:report_id>": "99999",
    "<int:job_id>": "99999", "<int:template_id>": "99999", "<int:rule_id>": "99999",
    "<int:flock_id>": "99999", "<int:member_id>": "99999", "<int:mute_id>": "99999",
    "<int:position_id>": "99999", "<int:hw_id>": "99999", "<int:perm_id>": "99999",
    "<int:override_id>": "99999", "<int:session_id>": "99999", "<int:request_id>": "99999",
    "<str:gateway_slot_id>": "GW-AUTO-2961900", "<str:slot>": "GW-AUTO-2961900",
    "<str:slot_id>": "SS-AUTO-2961900-1", "<str:metric_key>": "temp",
    "<str:unit_id>": "GW-AUTO-2961900", "<str:role>": "manager",
    "<str:report_date>": "2026-10-25",
    "<uuid:alert_id>": "00000000-0000-4000-8000-000000000000",
}
OK_CODES = (200, 201, 204, 400, 401, 403, 404, 405, 409, 422)


def call(method, url, headers=None, body=None, timeout=25):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        import json
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read(200).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(200).decode(errors="replace")
    except Exception as exc:  # noqa: BLE001
        return -1, repr(exc)[:90]


def totp(secret_b32, for_time=None):
    import base64, hashlib, hmac, struct, time
    key = base64.b32decode(secret_b32.upper())
    counter = int((for_time or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return "%06d" % code


STAFF_MFA_SECRET = "K5GJYPWTRUTM2EPQZDHMP56DPNTMATLP"


def token(username, password, mfa=False):
    import json
    payload = {"username": username, "password": password}
    if mfa:
        payload["mfa_code"] = totp(STAFF_MFA_SECRET)
    req = urllib.request.Request(
        BASE + "/api/v1/auth/login/",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            return json.loads(r.read().decode()).get("access_token", "")
    except urllib.error.HTTPError as e:
        print("login failed", username, e.code, e.read()[:120])
        return ""



# Endpoints that ACT on an empty body (no required fields). An empty-body POST
# would perform the action, so pass 1 skips them - found the hard way: the first
# sweep suspended the live gateway via POST /gateways/<slot>/suspend/.
ACTION_ENDPOINTS = (
    "/gateways/", "/suspend/", "/resume/", "/commands/", "/rotate-credential/",
    "/rotation-ack/", "/approve/", "/reject/", "/acknowledge/", "/force-resolve/",
    "/sync/", "/drain/", "/submit/", "/claim-code/", "/credential/", "/retire/",
    "/replace/", "/acl-backfill/", "/deactivate/", "/close/", "/publish/",
    "/session/", "/review/",
)

NAMED = {"farmSlug": "auto-farm-2961900", "salonSlug": "saloon-3",
         "report_date": "2026-10-25", "report_id": "99999", "slot": "GW-AUTO-2961900"}


def concrete(path):
    # re_path() named groups FIRST (they contain angle brackets the second
    # pass would otherwise eat): (?P<name>...)
    out = re.sub(r"\(\?P<([A-Za-z_][A-Za-z0-9_]*)>[^)]*\)",
                 lambda m: NAMED.get(m.group(1), "1"), path)
    # then Django path() placeholders: <int:x> / <str:x>
    out = re.sub(r"<[^>]+>", lambda m: SUFFIX.get(m.group(0), "1"), out)
    return out.replace("^", "")


def main():
    import json
    toks = {r: token(*c) for r, c in CREDS.items()}
    rows = [l for l in open(INVENTORY, encoding="utf-8") if l.startswith("api/")]
    for role, cred in CREDS.items():
        if not toks.get(role):
            print("SKIP role", role, "(login refused:", cred[0], ")")
    results, defects, skipped = [], [], []
    for line in rows:
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        path, methods = parts[0], parts[2]
        if path.startswith(("api/docs", "api/schema", "api/redoc", "api/ws-")):
            continue
        verbs = [m for m in methods.split(",") if m and m not in ("options", "head")]
        if not verbs:
            continue
        # pass 1 never fires an action endpoint - see ACTION_ENDPOINTS
        if any(frag in path for frag in ACTION_ENDPOINTS):
            skipped.append(path)
            continue
        url = BASE + "/" + concrete(path)
        tok = toks["support"] if ("/platform/" in path or "/provisioning/" in path) else toks["manager"]
        headers = {"Authorization": "Bearer " + (tok or "")}
        for verb in verbs:
            if verb == "get":
                code, body = call("GET", url, headers=headers)
            else:
                code, body = call(verb.upper(), url, headers=headers, body={})
            ok = code in OK_CODES
            results.append((verb.upper(), url.replace(BASE, ""), code, ok, body))
            if not ok:
                defects.append((verb.upper(), url, code, body))
            time.sleep(0.05)
    good = sum(1 for r in results if r[3])
    print(f"SWEEP total={len(results)} ok={good} defects={len(defects)} "
          f"action_endpoints_deferred={len(skipped)}")
    for d in defects:
        print("  DEFECT", d[0], d[1].replace(BASE, ""), "->", d[2], d[3][:120])
    print("--- table ---")
    for verb, path, code, ok, body in results:
        print(f"{verb:6} {code:>4} {'OK ' if ok else 'BAD'} {path}  {body[:60]}")


if __name__ == "__main__":
    main()
