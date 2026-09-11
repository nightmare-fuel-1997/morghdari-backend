#!/usr/bin/env python
"""emqx_provision.py — seed EMQX authentication + topic ACLs for the demo stack.

WHY it exists: the demo broker used to be wide open — any client could
publish to any topic, and the default file ACL source ended in `{allow, all}`
which would defeat any rule added on top. This provisioner makes the demo
honest, idempotently, through the EMQX management API:

  - per-farm simulator users (demo-sim-farm{id}) that may ONLY publish their
    OWN farm's telemetry + status topics (never subscribe, never other farms)
  - a subscriber user (demo-sub) that may ONLY subscribe the telemetry share
    group and the pack-liveness status topics (never publish)
  - authorization = built-in-DB source only (the file source is removed),
    no_match = deny, so anything not explicitly allowed is dropped

It runs on every `start_dev_stack.sh` start, so a recreated container is
re-secured automatically. Management API access uses the bootstrap API key
from scripts/emqx-api-keys.txt (EMQX 5.5's /api/v5 requires API keys; the
dashboard bootstrap users only cover the UI).

فارسی: آماده‌سازی بروکر دمو — هر پک شبیه‌ساز فقط تاپیک‌های مزرعهٔ خودش را
منتشر کند، مشترک فقط گروهِ اشتراکی را بگیرد، و مابقی رد شود.
"""

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request

AUTHN_CHAIN_ID = "password_based:built_in_database"


# ---------------------------------------------------------------------------
# Pure builders (import-safe, unit-tested)
# ---------------------------------------------------------------------------


def manifest_farm_ids(manifest: dict) -> list:
    """The farm ids that actually hold saloons (the manifest's farms list —
    the seed already excludes empty shell farms, so this is the board's truth)."""
    return sorted(f["id"] for f in manifest.get("farms", []))


def sim_username(farm_id, prefix: str = "demo-sim") -> str:
    return f"{prefix}-farm{farm_id}"


def sim_rules(farm_id: int) -> list:
    """A farm's simulator user may publish ONLY that farm's telemetry + status
    topics (org is a single-level wildcard; the farm segment is pinned)."""
    return [
        {"permission": "allow", "action": "publish",
         "topic": f"+/farms/{farm_id}/gateways/+/telemetry"},
        {"permission": "allow", "action": "publish",
         "topic": f"+/farms/{farm_id}/gateways/+/status"},
    ]


def subscriber_rules(subscriber_user: str = "demo-sub") -> list:
    """The subscriber may only SUBSCRIBE the telemetry lane and the
    pack-liveness status topics; it never publishes (no publish rules, so
    publish hits no_match=deny). The telemetry topic is the EFFECTIVE filter:
    EMQX checks shared-group subscriptions with the $share/group prefix
    STRIPPED (observed on 5.5 — a rule on the full $share form never matches,
    and the subscription is denied), so the rule must name what the broker
    actually checks. This still covers the subscriber's real subscription
    ($share/telemetry_workers/+/farms/+/gateways/+/telemetry)."""
    return [
        {"permission": "allow", "action": "subscribe",
         "topic": "+/farms/+/gateways/+/telemetry"},
        {"permission": "allow", "action": "subscribe",
         "topic": "+/farms/+/gateways/+/status"},
    ]


def desired_users(farms: list, sim_prefix: str = "demo-sim",
                  subscriber_user: str = "demo-sub", password: str = "devpass") -> list:
    """(username, password) pairs for every farm's simulator plus the subscriber."""
    out = [(sim_username(f, sim_prefix), password) for f in farms]
    out.append((subscriber_user, password))
    return out


def rules_by_username(farms: list, sim_prefix: str = "demo-sim",
                      subscriber_user: str = "demo-sub") -> dict:
    """username -> rule_items, exactly what the per-user rules API stores."""
    out = {sim_username(f, sim_prefix): sim_rules(f) for f in farms}
    out[subscriber_user] = subscriber_rules(subscriber_user)
    return out


# ---------------------------------------------------------------------------
# Minimal HTTP client for the EMQX management API (stdlib only)
# ---------------------------------------------------------------------------


def api_request(base_url: str, api_user: str, api_pass: str,
                method: str, path: str, body=None, timeout: int = 5):
    """Return (status, parsed_json_or_None). Never raises for HTTP errors —
    callers decide what a 404/409 means for idempotency."""
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    token = base64.b64encode(f"{api_user}:{api_pass}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode(errors="replace") if raw else None
        return exc.code, payload


class ProvisionError(Exception):
    pass


def _require_ok(api, method, path, body=None, ok=(200, 201, 204)):
    status, payload = api(method, path, body)
    if status not in ok:
        raise ProvisionError(f"{method} {path} -> HTTP {status}: {payload}")
    return status, payload


# ---------------------------------------------------------------------------
# Idempotent provisioning steps
# ---------------------------------------------------------------------------


def ensure_authn_chain(api) -> str:
    """The built-in-DB password chain must exist, be enabled and hash with
    BCRYPT (C7: app-stored $2b$ credentials and EMQX verify the same
    plaintext; the backend pushes plaintext at approval/rotation so the
    broker derives its own hash — either algorithm would verify, but bcrypt
    is the project's documented one). An existing chain with a different
    algorithm is deleted and recreated: its user hashes are sha256-derived
    and would never verify again; ensure_users() re-seeds them from the known
    demo plaintexts."""
    status, chains = api("GET", "/api/v5/authentication")
    if status != 200:
        raise ProvisionError(f"GET /api/v5/authentication -> HTTP {status}: {chains}")
    for chain in chains:
        if chain.get("id") != AUTHN_CHAIN_ID:
            continue
        algo = (chain.get("password_hash_algorithm") or {}).get("name")
        if algo == "bcrypt":
            return AUTHN_CHAIN_ID
        # Algorithm drift → wipe chain + users; recreation below re-seeds.
        _require_ok(api, "DELETE", f"/api/v5/authentication/{AUTHN_CHAIN_ID}",
                    ok=(200, 204, 404))
        break
    _require_ok(api, "POST", "/api/v5/authentication", {
        "mechanism": "password_based",
        "backend": "built_in_database",
        "user_id_type": "username",
        "password_hash_algorithm": {"name": "bcrypt"},
        "enable": True,
    })
    return AUTHN_CHAIN_ID


def ensure_users(api, chain_id: str, users: list) -> int:
    """Create missing users; returns how many were added (idempotent)."""
    status, payload = api("GET", f"/api/v5/authentication/{chain_id}/users")
    if status != 200:
        raise ProvisionError(f"GET users -> HTTP {status}: {payload}")
    existing = {u.get("user_id") for u in (payload or {}).get("data", [])}
    added = 0
    for username, password in users:
        if username in existing:
            continue
        status, payload = api(
            "POST", f"/api/v5/authentication/{chain_id}/users",
            {"user_id": username, "password": password},
        )
        if status not in (200, 201):
            raise ProvisionError(f"POST user {username} -> HTTP {status}: {payload}")
        added += 1
    return added


def ensure_authorization_settings(api) -> None:
    """Authorization ON, no_match=deny — anything not explicitly allowed is dropped."""
    status, settings = api("GET", "/api/v5/authorization/settings")
    if status != 200:
        raise ProvisionError(f"GET authorization/settings -> HTTP {status}: {settings}")
    settings = dict(settings or {})
    settings["enable"] = True
    settings["no_match"] = "deny"
    _require_ok(api, "PUT", "/api/v5/authorization/settings", settings)


def ensure_authorization_source(api) -> None:
    """Only the built-in-DB source may exist: the default FILE source ends in
    `{allow, all}`, which would let a non-matching publish fall through to
    allow — the exact hole per-farm rules must not have."""
    status, payload = api("GET", "/api/v5/authorization/sources")
    if status != 200:
        raise ProvisionError(f"GET authorization/sources -> HTTP {status}: {payload}")
    sources = (payload or {}).get("sources", [])
    for source in sources:
        if source.get("type") == "file":
            # 404 = already gone (another run removed it) — fine
            _require_ok(api, "DELETE", "/api/v5/authorization/sources/file", ok=(200, 204, 404))
    status, payload = api("GET", "/api/v5/authorization/sources")
    sources = (payload or {}).get("sources", [])
    if not any(s.get("type") == "built_in_database" for s in sources):
        _require_ok(api, "POST", "/api/v5/authorization/sources",
                    {"type": "built_in_database", "enable": True})


def ensure_rules(api, rules_by_user: dict) -> int:
    """PUT each user's rules (replaces — fully idempotent). Returns rule count."""
    total = 0
    for username, rules in rules_by_user.items():
        _require_ok(api, "PUT",
                    f"/api/v5/authorization/sources/built_in_database/rules/users/{username}",
                    {"username": username, "rules": rules})
        total += len(rules)
    return total


def provision(base_url: str, api_user: str, api_pass: str, manifest: dict,
              sim_prefix: str = "demo-sim", subscriber_user: str = "demo-sub",
              password: str = "devpass") -> dict:
    """Run the whole idempotent sequence; return a summary dict."""
    api = lambda method, path, body=None: api_request(base_url, api_user, api_pass, method, path, body)

    farms = manifest_farm_ids(manifest)
    users = desired_users(farms, sim_prefix, subscriber_user, password)
    rules = rules_by_username(farms, sim_prefix, subscriber_user)

    chain_id = ensure_authn_chain(api)
    users_added = ensure_users(api, chain_id, users)
    ensure_authorization_settings(api)
    ensure_authorization_source(api)
    rules_added = ensure_rules(api, rules)

    return {
        "chain_id": chain_id,
        "farms": farms,
        "users": [u for u, _ in users],
        "users_added": users_added,
        "rules_added": rules_added,
        "no_match": "deny",
    }


def build_parser() -> argparse.ArgumentParser:
    # ASCII description: the module docstring carries Persian comments, and
    # argparse would crash printing them on a cp1252 console (--help).
    parser = argparse.ArgumentParser(
        prog="emqx_provision",
        description=(
            "Seed EMQX authn users + topic ACLs for the demo stack "
            "(per-farm sim users, subscribe-only subscriber, no_match=deny). "
            "Idempotent — safe to run on every stack start."
        ),
    )
    parser.add_argument("--manifest", required=True,
                        help="farm topology manifest the operator supplies "
                             "(ids of the farms/saloons the sim users are scoped to)")
    parser.add_argument("--base-url", default="http://127.0.0.1:18083")
    parser.add_argument("--api-user", default="demo-admin",
                        help="management-API key (scripts/emqx-api-keys.txt)")
    parser.add_argument("--api-pass", default="dev_password")
    parser.add_argument("--password", default="devpass",
                        help="MQTT password for every provisioned user (demo convention)")
    parser.add_argument("--sim-prefix", default="demo-sim",
                        help="simulator username prefix; per-farm users become {prefix}-farm{id}")
    parser.add_argument("--subscriber-user", default="demo-sub")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with open(args.manifest, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except OSError as exc:
        print(f"cannot read manifest {args.manifest!r}: {exc}", file=sys.stderr)
        return 1
    try:
        summary = provision(args.base_url, args.api_user, args.api_pass, manifest,
                            args.sim_prefix, args.subscriber_user, args.password)
    except ProvisionError as exc:
        print(f"provisioning failed: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"EMQX management API unreachable at {args.base_url}: {exc}", file=sys.stderr)
        return 1

    print(f"EMQX provisioned: authn chain {summary['chain_id']} "
          f"(+{summary['users_added']} new users), "
          f"{summary['rules_added']} ACL rules for {len(summary['farms'])} farms "
          f"(no_match={summary['no_match']})")
    print("  users: " + ", ".join(summary["users"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
