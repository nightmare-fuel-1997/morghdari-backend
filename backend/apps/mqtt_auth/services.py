"""MQTT credential + ACL services — M5-06/07/08 broker enforcement.

M5-06 gateway config: contract-safe payload (org/farm ids, credential shape,
rate-limit note) built from the slot + credential rows — the plaintext
password is issued EXACTLY once at request approval; later fetches return the
bcrypt hash plus a rotation_pending flag.

M5-07 EMQX ACL wiring: the backend's source-of-truth is `mqtt_acl_rules`
(allow-rows written when a request is approved). `sync_gateway_acls` pushes
them into EMQX's built-in-DB authorizer (no_match=deny is a broker-level
setting applied at stack bootstrap); `revoke_gateway_acls` empties a suspended
or rejected gateway's rules so every publish/subscribe falls to deny. EMQX
failures are logged, FNV-soft: the DB state remains the truth.

M5-08 staged rotation: `stage_rotation` stores a bcrypt pending hash (the old
password keeps working at the broker — no lockout), `commit_pending_rotation`
promotes the pending hash ONLY on device ACK (the ack carries the new
password, proving the device received it), and `alert_stale_rotations`
(beat-driven) flags rotations staged longer than 24 h for operator follow-up.

Hashes: native bcrypt $2b$ — the only format EMQX 5.5's
password_based:bcrypt backend verifies natively (C7 / AU-fix). Django's
bcrypt_sha256 wrappers from earlier flows are still accepted by
`verify_password` for ACK checks.

فارسی: اعتبارنامه و قواعد ACL دستگاه‌ها — همگام‌سازی قواعد روی EMQX، ابطال در
تعلیق/رد، و چرخش پلکانی اعتبارنامه که فقط با تأیید دستگاه اعمال می‌شود.
"""

import json
import logging
import os
import urllib.error
import urllib.request

from django.utils import timezone

from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential

logger = logging.getLogger(__name__)

STALE_ROTATION_HOURS = 24


# ---------------------------------------------------------------------------
# bcrypt helpers (EMQX-native)
# ---------------------------------------------------------------------------

def hash_password(raw: str) -> str:
    """bcrypt $2b$ hash — EMQX password_based:bcrypt native format (C7)."""
    import bcrypt

    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(raw: str, stored: str) -> bool:
    """Verify a presented password against a stored hash. Accepts native
    bcrypt AND legacy Django bcrypt_sha256/pbkdf2 wrappers (old flows)."""
    if not stored:
        return False
    if stored.startswith(("bcrypt_sha256$", "pbkdf2_", "argon2")):
        from django.contrib.auth.hashers import check_password

        return check_password(raw, stored)
    try:
        import bcrypt

        return bcrypt.checkpw(raw.encode("utf-8"), stored.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# M5-06: gateway config payload
# ---------------------------------------------------------------------------

def gateway_config_payload(slot, credential: MqttDeviceCredential | None) -> dict:
    """R2 contract-safe config: org/farm identity, MQTT endpoint, credential
    shape (hash only — plaintext is one-time), rotation_pending flag, and an
    nginx rate-limit note. Deliberately NO sensor slot_ids in the payload."""
    return {
        "gateway_slot_id": slot.slot_id,
        "org_id": str(slot.org_id),
        "farm_id": slot.farm_id,
        "provisioning_status": slot.provisioning_status,
        "mqtt": {
            "host": os.environ.get("EMQX_HOST", "127.0.0.1"),
            "port": int(os.environ.get("EMQX_PORT", "1883")),
            "tls": os.environ.get("EMQX_TLS", "0") == "1",
        },
        "auth": {
            "username": credential.username if credential else None,
            "password_hash": credential.password_hash if credential else None,
            # The plaintext password is returned ONCE (request approval /
            # credential rotation); fetches after that see only the hash.
            "password_issued_once": True,
            "rotation_pending": bool(credential and credential.pending_password_hash),
        },
        "rate_limit_note": (
            "Throttle MQTT CONNECT + publish rate per gateway at the "
            "reverse-proxy layer (nginx limit_req keyed on client ip); "
            "broker-level quotas land with the v1.0 quota engine."
        ),
    }


# ---------------------------------------------------------------------------
# M5-07: EMQX ACL sync / revocation
# ---------------------------------------------------------------------------

def _emqx_base_url() -> str:
    return os.environ.get("EMQX_API_URL", "http://127.0.0.1:18083").rstrip("/")


_emqx_token = {"value": None}  # process-cached dashboard JWT


def _emqx_login() -> str | None:
    """POST /api/v5/login with the dashboard credentials (EMQX 5.x accepts
    no Basic auth on /api/v5 — dashboard users trade password for a JWT).
    Cached per process; cleared and retried once on 401."""
    if _emqx_token["value"]:
        return _emqx_token["value"]
    body = json.dumps(
        {
            "username": os.environ.get("EMQX_API_USER", "admin"),
            "password": os.environ.get("EMQX_API_PASSWORD", ""),
        }
    ).encode()
    req = urllib.request.Request(
        _emqx_base_url() + "/api/v5/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            _emqx_token["value"] = json.loads(resp.read()).get("token")
    except OSError as exc:
        logger.warning("EMQX unreachable (%s): login", exc)
    return _emqx_token["value"]


def _emqx_request(method: str, path: str, body=None, timeout: int = 5):
    """Minimal stdlib EMQX management-API client (same dialect as
    scripts/emqx_provision.py). Returns (status, payload); never raises for
    HTTP errors — callers decide what a 404 means for idempotency."""
    token = _emqx_login()
    if token is None:
        return 0, None
    def _do(jwt):
        url = _emqx_base_url() + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {jwt}")
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
        except OSError as exc:
            logger.warning("EMQX unreachable (%s): %s %s", exc, method, path)
            return 0, None

    status, payload = _do(token)
    if status == 401:  # expired/revoked JWT — re-login once and retry
        _emqx_token["value"] = None
        token = _emqx_login()
        if token is not None:
            return _do(token)
    return status, payload


def _rules_endpoint(username: str) -> str:
    import urllib.parse

    return (
        f"/api/v5/authorization/sources/built_in_database/rules/users/"
        f"{urllib.parse.quote(username, safe='')}"
    )


def _users_endpoint(username: str | None = None) -> str:
    import urllib.parse

    base = f"/api/v5/authentication/{AUTHN_CHAIN_ID}/users"
    if username is None:
        return base
    return base + "/" + urllib.parse.quote(username, safe="")


AUTHN_CHAIN_ID = "password_based:built_in_database"

_builtin_chain = {"known": None}  # None=unprobed; EMQX mgmt probe is cached


def builtin_chain_exists() -> bool:
    """Whether the broker actually has the built-in-DB authn chain.

    PROD-FIX 2026-09-19: production EMQX is postgres-only by design
    (emqx.conf: authn + authz both postgresql; the built_in_database chain
    exists only under the dev-only compose override). Pushing users/rules to
    a nonexistent chain 400s forever, so every built-in-DB sync below is
    skipped quietly when the chain is absent — postgres stays the single
    source of truth and enforcement moves to the DB rows (see suspend/
    reject in provisioning/services.py).

    An UNREACHABLE broker (probe status 0) is never a chain verdict: it is
    not cached, and callers keep their pre-2026-09-19 FNV-soft paths — so a
    down dev broker still reads as failure, while an answering postgres-only
    broker reads as skip. `api_hardware` relies on this: `reachable` stays
    honest for the replacement ceremony's broker_synced flag.
    """
    if _builtin_chain["known"] is None:
        status, payload = _emqx_request("GET", "/api/v5/authentication")
        if status == 0:
            return False
        ids = set()
        if status == 200:
            chains = payload if isinstance(payload, list) else [payload]
            ids = {c.get("id") for c in chains if isinstance(c, dict)}
        _builtin_chain["known"] = AUTHN_CHAIN_ID in ids
        if not _builtin_chain["known"]:
            logger.info(
                "EMQX has no %s chain (postgres-only authn); built-in-DB "
                "user/ACL sync skipped",
                AUTHN_CHAIN_ID,
            )
    return _builtin_chain["known"]


def ensure_gateway_user(username: str, plaintext: str) -> bool:
    """Create the gateway's EMQX authn user with the PLAINTEXT password.

    EMQX 5.5's built-in-DB users API accepts only `password` (pre-hashed
    `password_hash` is rejected — verified live 2026-09-02), so the plaintext
    is pushed at exactly the two moments the backend holds it: request
    approval (one-time issue) and rotation ACK (the device presents the new
    password). The app DB keeps the $2b$ hash for HTTP Basic verification;
    the broker derives its own hash from the same plaintext — one credential,
    two verifiers (C7). FNV-soft: a broker outage never blocks approval.
    Skipped entirely when the broker has no built-in-DB chain (postgres-only
    production authn)."""
    if not builtin_chain_exists():
        return True
    status, _payload = _emqx_request(
        "POST", _users_endpoint(),
        {"user_id": username, "password": plaintext},
    )
    if status in (200, 201):
        return True
    if status == 409:  # user already exists with this identity — fine
        return True
    logger.warning(
        "EMQX user creation failed for username=%s (HTTP %s) — device cannot "
        "authenticate until created (rotation ACK recreates it)",
        username, status,
    )
    return False


def update_gateway_user_password(username: str, plaintext: str) -> bool:
    """Push a rotated password to EMQX (rotation commit's broker step).
    PUT updates the existing user; on 404 (user was never created or was
    wiped) fall back to POST, which heals the missing user — a rotation ACK
    therefore self-converges a broken broker state. Skipped when the broker
    has no built-in-DB chain (postgres-only production authn)."""
    if not builtin_chain_exists():
        return True
    status, _payload = _emqx_request(
        "PUT", _users_endpoint(username), {"password": plaintext},
    )
    if status == 200:
        return True
    if status == 404:
        return ensure_gateway_user(username, plaintext)
    logger.warning(
        "EMQX password update failed for username=%s (HTTP %s)",
        username, status,
    )
    return False


def canonical_gateway_acl_rules(
    slot_id: str,
    org_id: str | None = None,
    farm_id: int | str | None = None,
) -> list[dict]:
    """The canonical MQTT ACL rule builder across the gateway lifecycle.

    Returns the exact list of ACL rules (permission, action, topic):
    - Unbound (org_id or farm_id is None): 4 device-lane bootstrap rules.
    - Bound (org_id and farm_id present): 13 rules (4 device + 9 tenant).
    Strictly literal topics; zero wildcards.
    """
    from apps.provisioning import topics
    from apps.provisioning.api import gateway_acl_rule_items

    device_rules = [
        {"topic": topics.desired_topic(slot_id), "permission": "allow", "action": "subscribe"},
        {"topic": topics.command_topic(slot_id), "permission": "allow", "action": "subscribe"},
        {"topic": topics.reported_topic(slot_id), "permission": "allow", "action": "publish"},
        {"topic": topics.command_response_topic(slot_id), "permission": "allow", "action": "publish"},
    ]
    if org_id is None or farm_id is None:
        return device_rules

    tenant_rules = gateway_acl_rule_items(str(org_id), farm_id, slot_id)
    return device_rules + tenant_rules


def bootstrap_acl_rule_items(unit_id: str) -> list[dict]:
    """The FOUR rules a factory-registered, unbound unit needs — and no more.

    AD-1/AD-18 (2026-10-18): before binding, a unit has no org, so it cannot be
    permitted anywhere near a tenant topic. Its entire broker surface is its own
    device lane: read the desired config, read one-shot commands, report back.

    FOUND 2026-10-20 (the third instance of the D9 tail — subscribe/subscribed
    but never AUTHORIZED): the fourth rule, publish on `command/response`, was
    missing. The command channel (Phase 3) cannot work without it: a pre-bind
    unit's ACK and SUCCESS replies were denied by `no_match=deny`, so a command
    answered before binding simply never reached us. The same shape of gap had
    already broken the status lane (D1) and the command lane itself (D9) — the
    lesson is that a lane added on one side needs its permission on BOTH sides,
    and every one of the three lists that has to agree is a place it can drift.

    Compare with `gateway_acl_rule_items()` (provisioning/api.py), which grants
    the full T1–T9 tenant set — that set is added at BIND time, after the unit
    has an org and a farm. Keeping the two functions separate is what makes the
    pre-binding state strictly less privileged than a provisioned gateway.
    """
    return canonical_gateway_acl_rules(unit_id)


def sync_gateway_acls(slot) -> dict:
    """Push the slot's mqtt_acl_rules allow-rows into EMQX's built-in-DB
    authorizer. Returns {"synced": n, "reachable": bool}; on broker failure
    the DB rules remain the source of truth (logged, never raised). Also
    probes the authn user: an absent user (approval-time broker hiccup or a
    wiped broker) is logged — the next credential rotation recreates it via
    update_gateway_user_password's 404-heal. On postgres-only brokers the
    DB allow-rows ARE the enforcement (no built-in chain to push to), so the
    count is affirmed without a broker round-trip."""
    credential = MqttDeviceCredential.objects.filter(username=slot.slot_id).first()
    if credential is None:
        return {"synced": 0, "reachable": True}
    if not builtin_chain_exists():
        rules = MqttAclRule.objects.filter(username=credential.username).count()
        return {"synced": rules, "reachable": True}
    probe_status, _ = _emqx_request("GET", _users_endpoint(credential.username))
    if probe_status == 404:
        logger.warning(
            "EMQX authn user %s missing — approval-time creation may have "
            "failed; rotate the credential to recreate it",
            credential.username,
        )
    rules = [
        {"permission": r.permission, "action": r.action, "topic": r.topic}
        for r in MqttAclRule.objects.filter(username=credential.username)
    ]
    status, _payload = _emqx_request(
        "PUT", _rules_endpoint(credential.username),
        {"username": credential.username, "rules": rules},
    )
    if status not in (200, 201, 204):
        logger.warning(
            "EMQX ACL sync failed for username=%s (HTTP %s) — DB rules kept",
            credential.username, status,
        )
        return {"synced": 0, "reachable": False}
    return {"synced": len(rules), "reachable": True}


def revoke_gateway_acls(slot) -> bool:
    """M5-07 suspension revocation: empty the gateway's EMQX rules so no_match
    = deny drops every publish/subscribe until resume re-syncs the allows.
    The AUTHN user is deliberately kept: resume has no plaintext to recreate
    it (the password is issued exactly once), and a kept user without rules
    cannot publish — while rotation's 404-heal can still repair a wiped
    broker. (Broker ACL-cache TTL, default 1m, bounds revocation latency for
    an already-connected client — documented in TASKS M5-07.)

    On postgres-only brokers there is no built-in chain to empty; enforcement
    is the DB allow-rows, deleted by the caller (provisioning/services
    suspend/reject) — this push is then skipped."""
    credential = MqttDeviceCredential.objects.filter(username=slot.slot_id).first()
    if credential is None:
        return True
    if not builtin_chain_exists():
        return True
    status, _payload = _emqx_request(
        "PUT", _rules_endpoint(credential.username),
        {"username": credential.username, "rules": []},
    )
    if status not in (200, 201, 204):
        logger.warning(
            "EMQX ACL revoke failed for username=%s (HTTP %s)",
            credential.username, status,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# M5-08: staged credential rotation
# ---------------------------------------------------------------------------

def stage_rotation(username: str, raw_password: str) -> MqttDeviceCredential:
    """Stage a rotation: the new password is hashed into pending_password_hash
    while the OLD hash stays active at the broker — zero lockout risk. The
    plaintext is returned by the caller to be installed on the device out of
    band (the ack proves receipt)."""
    credential = MqttDeviceCredential.objects.get(username=username)
    credential.pending_password_hash = hash_password(raw_password)
    credential.pending_since = timezone.now()
    credential.save(update_fields=["pending_password_hash", "pending_since"])
    logger.info("rotation staged for username=%s (old credential stays active)", username)
    return credential


def commit_pending_rotation(username: str, presented_password: str) -> bool:
    """Commit-on-ACK: promote the pending hash ONLY when the device presents
    the new password (proof it received the rotated credential), then push
    the new password to the EMQX authn user (the broker must re-auth the
    device under the new credential). False when nothing is staged or the
    presented password does not match — the EMQX push itself is FNV-soft
    (logged; the next rotation retries the 404-heal path)."""
    credential = MqttDeviceCredential.objects.filter(username=username).first()
    if credential is None or not credential.pending_password_hash:
        return False
    if not verify_password(presented_password, credential.pending_password_hash):
        return False
    credential.password_hash = credential.pending_password_hash
    credential.pending_password_hash = None
    credential.pending_since = None
    credential.last_rotated_at = timezone.now()
    credential.save(update_fields=[
        "password_hash", "pending_password_hash", "pending_since", "last_rotated_at",
    ])
    logger.info("rotation committed for username=%s (ACK verified)", username)
    if not update_gateway_user_password(username, presented_password):
        logger.warning(
            "rotation committed in DB for username=%s but EMQX still holds "
            "the old password — device re-auth will fail until the broker "
            "catches up (rotate again or re-run sync)",
            username,
        )
    return True


def alert_stale_rotations(now_dt=None, max_age_hours: int = STALE_ROTATION_HOURS) -> int:
    """Beat sweep: stage a rotation longer than max_age_hours without a device
    ACK (device lost, operator forgot) — logged for follow-up; returns count.
    Real paging lands with the v0.6 delivery channels."""
    from datetime import timedelta

    cutoff = (now_dt or timezone.now()) - timedelta(hours=max_age_hours)
    stale = list(
        MqttDeviceCredential.objects.filter(
            pending_password_hash__isnull=False,
            pending_since__lt=cutoff,
        ).values_list("username", "pending_since")
    )
    for username, pending_since in stale:
        logger.warning(
            "M5-08: credential rotation staged >%dh without device ACK — "
            "username=%s pending_since=%s",
            max_age_hours, username, pending_since,
        )
    return len(stale)