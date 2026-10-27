# Morghdari MQTT Device Protocol — Complete Integration Guide (v2.0)

**To:** hardware / firmware team
**From:** Morghdari software team (backend)
**Date:** 2026-10-27
**Status:** Authoritative for all device ↔ broker communication. This document
merges and supersedes the topic/transport portions of the earlier
`Gateway Firmware Specification` and `hardware-firmware-requirements-v1.md`
(2026-10-19); §3–§8 of that document remain valid and are incorporated here
unchanged where they overlap.

Everything in this document is verified against the deployed production broker
(EMQX 5.5 on the VPS) and the backend subscribers that consume these lanes as
of tag `vps-17`.

---

## 1. System overview

```
                        ┌──────────────────────────── VPS ────────────────────────────┐
                        │                                                             │
  ┌──────────┐  TLS 8883 │  ┌──────┐   shared subs   ┌──────────────┐    ┌──────────┐ │
  │ GATEWAY  │──────────▶│  │ EMQX │────────────────▶│  subscriber  │───▶│  Redis   │ │
  │ (device) │◀──────────│  │ 5.5  │                 │  (2 replicas)│    │  streams │ │
  └──────────┘           │  └──────┘                 └──────────────┘    └────┬─────┘ │
   sensors via           │      ▲  ▲                                           ▼       │
   LoRa packs            │      │  │  desired/commands          ┌────────────────────┐  │
   (pack_id)             │      │  └────────────────────────────│  celery + django   │  │
                         │      └───────────────────────────────│  (REST API + WS)   │  │
                         │         (retained docs, commands)    └─────────┬──────────┘  │
                         │                                                ▼             │
                         │                                        ┌──────────────┐      │
                         │              authn/authz lookups ─────▶│  PostgreSQL  │      │
                         │                                        └──────────────┘      │
                         └─────────────────────────────────────────────────────────────┘
```

What you build: **the gateway box** (left) and its LoRa sensor packs. Everything
else already runs and is not your concern — but the topics, payload schemas and
behaviour rules below are a strict contract: messages that violate them are
dead-lettered, not silently fixed.

---

## 2. Connection specification

| Parameter | Value | Notes |
|---|---|---|
| Host | provided per deployment (e.g. `82.115.17.252` / `mqtt.morghdari.com`) | in the desired document too |
| Port | **8883** | plaintext 1883 is **disabled** in production |
| TLS | **required**, server cert verified (`verify_peer`) | client certificates optional in Phase 1 — identity is username/password (bcrypt) |
| Protocol | **MQTT 3.1.1 (level 4)** | broker **rejects MQTT 5.0** at CONNECT (`max_protocol_version = 4`). Pin `MQTTv311` explicitly. |
| Username | your `unit_id` (e.g. `GW-2026-0042`) | |
| Password | 43 random chars, burned in factory | bcrypt-hashed server-side; never transmitted anywhere else |
| Keepalive | 30 s (reference implementation value; anything reasonable accepted) | |
| Clean session | recommended `False` (persistent) | broker keeps your session **24 h** (`session_expiry_interval`) |
| Client ID | any (≤128 chars); convention: prefix with the unit_id | uniqueness enforced by broker |

### 2.1 Broker-side limits (enforced, plan your firmware around them)

| Limit | Value |
|---|---|
| Max packet size | 1 MB |
| Max topic levels | 10 |
| Connection rate | 50/s (fleet-wide limiter — reconnect storms throttle; backoff!) |
| Message rate | 20/s per connection |
| Bandwidth | 100 KB/s per connection |
| Session expiry | 24 h |

### 2.2 Reconnect behaviour (required)

Retry **forever** with exponential backoff + jitter (e.g. 1 s → 2 s → 4 s …
capped at 5 min). A device that cannot reach the broker must never give up,
never require a physical reset, and never stagger a fleet-wide reconnect (add
random jitter so 500 units recovering from an outage do not all hit the 50/s
limiter in the same second).

On every reconnect you must: re-subscribe to your lanes (§4), re-publish
retained `status: ONLINE` (§8.4), and re-report the applied `config_version`
(§7.2). The retained `desired` document is re-delivered automatically by the
broker on subscribe — that is your "request config" and you never need another.

---

## 3. The four identities

| Identity | Example | Assigned by | Stored where | Changes? |
|---|---|---|---|---|
| `unit_id` (= `slot_id`, = `gateway_slot_id`) | `GW-2026-0042` | software team, at registration | device flash, broker, database | **never** |
| MQTT password | 43 random chars | software team, once | **device flash only**; bcrypt hash in DB | rotatable via command |
| `claim_code` | 22 random chars | software team, once | **printed on the QR label only** | single use, installation token |
| `pack_id` | `a1b2c3d4` (8 hex) | burned into each LoRa sensor at manufacture | printed on the sensor | never; recorded by the installer in the backend |

Rules that are non-negotiable:

1. **The device knows itself from flash.** Nothing at install time tells it its
   identity — that is what lets it connect before any farm exists.
2. **The password never leaves the factory line.** Never printed, never in the
   QR, never typed by an installer.
3. **The QR carries `unit_id` + `claim_code`** (plus human-readable `unit_id`).
   The claim code is a one-time installation token, not a credential.

### 3.1 What the factory line does

1. Call the registration API — **one call returns `unit_id`, password and
   `claim_code`, exactly once** (`POST /api/v1/platform/devices/units/`;
   per-unit credentials and claim codes via `/credential/` and `/claim-code/`).
2. Write `unit_id` + password into **encrypted flash**.
3. Print the label / QR (§3).
4. Destroy the plaintext password from production-line memory.

A device must power on and connect using nothing but its flash contents.

---

## 4. Topic map — complete reference

Two namespaces exist. The **device lanes** work before you belong to any
organisation (you always have your `unit_id`). The **tenant lanes** open after
binding, once the desired document tells you your `org_id` and `farm_id`.

### 4.1 Device lanes (pre-bind) — Category A + the device-lane response

| Topic | Direction | Retained | QoS | Purpose |
|---|---|---|---|---|
| `devices/{unit_id}/desired` | server → you | **yes** | 1 | full configuration document (§7) |
| `devices/{unit_id}/command` | server → you | no | 1 | one-shot commands (§8.2) |
| `devices/{unit_id}/reported` | you → server | no | 1 | config-apply reports / heartbeat (§8.1) |
| `devices/{unit_id}/command/response` | you → server | no | 1 | your answers to `command` envelopes (§8.2) |

Subscribe to **exactly these four at boot** (before any org exists). Your ACL
in this state contains nothing else: **you cannot publish telemetry yet** —
there is no tenant topic you are permitted to touch. That is deliberate
(AD-3): an unbound device has no org, so a tenant topic for it cannot even be
composed, and telemetry before binding is meaningless.

`command/response` is deliberately separate from `reported`: a command answer
belongs to exactly one `command_uuid`, while a report describes current state.

### 4.2 Tenant lanes (post-bind)

`{org}` is the org UUID, `{farm}` the farm integer id, `{slot}` your
`unit_id`/slot — the desired document carries all three AND repeats the full
topic set so you can cross-check (§7).

| Topic | Direction | Retained | QoS | Purpose |
|---|---|---|---|---|
| `{org}/farms/{farm}/gateways/{slot}/telemetry` | you → server | no | 1 | sensor readings (§8.3) |
| `{org}/farms/{farm}/gateways/{slot}/status` | you → server | **yes** | 1 | liveness `ONLINE`/`OFFLINE` + LWT (§8.4) |
| `{org}/farms/{farm}/gateways/{slot}/commands` | server → you | no | 1 | commands, same envelope as device lane |
| `{org}/farms/{farm}/gateways/{slot}/commands/response` | you → server | no | 1 | your command answers (post-bind lane) |
| `…/clock/sync`, `…/ota`, `…/diagnostics/*` | both | varies | 1 | auxiliary lanes (server-side rollout in progress; ignore gracefully until a `desired`/`command` tells you otherwise) |

### 4.3 Identity model enforced by the server (why topic honesty matters)

For tenant telemetry the server takes `org`/`farm`/`slot` **from the topic**
(the broker already authenticated you and your ACL pins your topics — the topic
is unforgeable evidence). The payload's single mirrored field
`gateway_slot_id` **must equal the topic's slot segment**. A disagreement is an
identity fault → dead-letter. Same rule for `status` (§8.4).

---

## 5. Provisioning lifecycle — what the device sees

```
 factory              installation                approval               live
┌──────────┐   ┌────────────────────────┐   ┌──────────────────┐   ┌──────────────┐
│REGISTERED│──▶│ PENDING_INSTALLATION / │──▶│ PENDING_APPROVAL │──▶│ PROVISIONED  │
│ (flash + │   │  install job runs,     │   │ (telemetry HELD  │   │ (full tenant │
│  connect)│   │  device bound to farm) │   │  in FIFO lane)   │   │  telemetry)  │
└──────────┘   └────────────────────────┘   └──────────────────┘   └──────────────┘
                     │                                                  │
                     │         REJECTED / SUSPENDED (operator action)   │
                     └──────────────────▶ archived w/ reason, never silent
```

What this means **for the device**:

1. **You can CONNECT at any state** shown above (and even before, as a
   REGISTERED factory unit with no slot yet — the broker auth LEFT-JOINs the
   unit table for exactly that case).
2. **PENDING_APPROVAL holds your telemetry, it does not lose it.** Keep
   publishing on the tenant lane as soon as the desired document says so; the
   backend buffers everything in a FIFO stream until the farm manager approves.
   After approval the buffered telemetry is **replayed** into the normal
   pipeline, with original `device_ts` preserved (§8.3). You do nothing
   special — this is entirely server-side.
3. **REJECTED/SUSPENDED** gateways' telemetry is archived with a reason
   (visible to operators). It is never silently dropped — but stop expecting
   processing until you are resumed.
4. The desired document arrives whenever binding completes on our side —
   **do not assume the device is online when it is installed**; the config
   lands whenever you next connect (retained).

---

## 6. Trust & auth mechanics (informational — explains the "why")

- Authn: EMQX `password_based` / `postgresql` backend, bcrypt hashes. The auth
  query requires your unit to be in a provisioning state that permits
  connection (`PENDING_INSTALLATION`, `PENDING_APPROVAL`, `PROVISIONED`) or to
  be a REGISTERED/BOUND factory unit with no slot yet. A retired/revoked
  credential (`is_active = false`) cannot connect.
- Authz: per-username allow rows in `mqtt_acl_rules`, **no wildcards on device
  rows** — every row pins your literal topics. Deny-by-default
  (`no_match = deny`): a publish/subscribe your ACL does not name is refused.
  Publishing telemetry from an unbound device is therefore *impossible*, not
  merely discouraged (the reference simulator asserts this as a test).
- All of this means: **never publish on a topic you have not been told to use,
  never subscribe with wildcards.** The broker will refuse, and a firmware that
  treats refusal as normal will hide real faults.

---

## 7. The desired-state document (server → device)

### 7.1 Schema (`devices/{unit_id}/desired`, retained, QoS 1)

```json
{
  "schema_version": "1.0",
  "config_version": 42,
  "slot_id": "GW-2026-0042",
  "org_id": "9f8c2d34-1a2b-4c3d-8e5f-0a1b2c3d4e5f",
  "farm_id": 17,
  "farm_name": "Shahriar Farm",
  "gateway_hardware_id": "AA:BB:CC:DD:EE:FF",
  "mqtt": {"host": "mqtt.morghdari.com", "port": 8883, "tls": true},
  "sensors": [
    {"pack_id": "a1b2c3d4", "slot_id": "SS-01", "saloon_id": 3,
     "saloon_name": "Saloon 1", "position": "North Wall",
     "label": "Saloon 1 - North Wall", "metrics": ["temp", "humidity"]}
  ],
  "report_interval_sec": 60,
  "topics": {
    "telemetry": "{org}/farms/{farm}/gateways/{slot}/telemetry",
    "status": "{org}/farms/{farm}/gateways/{slot}/status",
    "commands": "{org}/farms/{farm}/gateways/{slot}/commands",
    "commands_response": "{org}/farms/{farm}/gateways/{slot}/commands/response",
    "desired": "devices/{unit_id}/desired",
    "reported": "devices/{unit_id}/reported",
    "command": "devices/{unit_id}/command"
  },
  "server_ts": "2026-10-27T09:15:00Z"
}
```

`report_interval_sec` defaults to 60 on the server (system-configurable).

### 7.2 Required device behaviour

This is the single most important design property: **the document is complete,
never a delta.** It is published retained, so it must be sufficient to rebuild
your whole state from a wiped flash. **Replace** your sensor list, interval and
topic set with what it says — do not merge.

1. If `slot_id` ≠ your `unit_id` → **ignore the document**, log a fault. Never
   act on another device's document.
2. If `config_version` ≤ last applied → **idempotent no-op** (monotonic guard;
   retained re-delivery is normal, not a bug).
3. Apply the **whole** document; derive each topic string yourself from
   `org_id`/`farm_id`/`slot_id` and **compare with `topics`** — a mismatch is a
   mis-addressed document: refuse it rather than publish into a topic nobody
   reads (AD-7).
4. Persist the applied `config_version` to flash **before** reporting.
5. Publish the report (§8.1).

The document **contains no secrets** — no password, no claim code. Safe to log
and diff between versions.

---

## 8. Payload specifications (device ↔ server)

### 8.1 `reported` — config report (device → server)

```json
{"schema_version": "1.0", "slot_id": "GW-2026-0042", "config_version": 42,
 "state": "APPLIED", "error": null, "ts": "2026-10-27T09:15:00Z"}
```

- `state`: `APPLIED` or `FAILED`.
- On `FAILED`, `error` carries a short human-readable reason — it is surfaced
  verbatim to operators; write it clearly.
- QoS 1. Retry the report if the publish fails; the server keeps republishing
  the retained `desired` until it sees your report, so convergence is
  guaranteed either way.

### 8.2 Commands — envelope, three-event protocol

Envelope (device lane pre-bind, tenant `commands` lane post-bind — same shape):

```json
{"schema_version": "1.0", "command_uuid": "…uuid…", "action": "REFRESH_CONFIG",
 "params": {}, "expires_at": "2026-10-28T09:20:00Z"}
```

Reply on the matching `…/command/response` (device lane) or
`…/commands/response` (tenant lane), correlated by `command_uuid`:

Five rules, all learned the hard way from the reference simulator:

1. **Answer every command, including an expired one** (`EXPIRED`). Silence
   leaves the command journal `SENT` until the server sweep expires it — a
   reply we can correlate is always better than silence.
2. **Three events, never collapsed**: publish `ACK` first (envelope accepted),
   then act, then `SUCCESS`/`FAILED`. Separate publishes — the server records
   each state transition separately. An `ACK`-only reply tells an operator the
   device is alive and still working.
3. **`expires_at` is UTC.** If a timestamp carries no offset, treat it as UTC —
   never local time. (Our reference device once compared a UTC clock as local
   on a +03:30 machine and concluded every command had expired 3.5 h early.)
4. **An unknown action is a `FAILED` reply** with a short reason — never a
   silent drop.
5. **`ROTATE_CREDENTIAL` is special**: reply `SUCCESS` with the new password
   inside `result` — `{"state":"SUCCESS","result":{"password":"<new>"}}`. The
   server does not commit the new password until it sees that proof. **Do not
   reconnect during rotation** — the broker still holds the old password until
   commit; adopt the new credential, prove it, reconnect on your next natural
   reconnect.

### 8.3 Telemetry (device → server, tenant lane) — schema 2.0

```json
{
  "gateway_hardware_id": "AA:BB:CC:DD:EE:FF",
  "gateway_slot_id": "GW-2026-0042",
  "schema_version": "2.0",
  "device_ts": "2026-10-27T09:15:00Z",
  "is_buffered": false,
  "clock_drift": false,
  "readings": [
    {"pack_id": "a1b2c3d4",
     "metrics": {
       "temp":     {"avg": 28.0, "min": 26.0, "max": 30.0},
       "humidity": {"avg": 61.5, "min": 58.0, "max": 64.0},
       "nh3":      {"avg": 14.0, "min": 10.0, "max": 20.0, "max_ts": 47}
     }}
  ]
}
```

**Field rules (all enforced by pipeline gates — violations dead-letter):**

| Field | Rule |
|---|---|
| `gateway_slot_id` | **must equal the topic's slot segment** (identity fault otherwise) |
| `schema_version` | exactly `"2.0"` — any other value is rejected (`UNSUPPORTED_SCHEMA_VERSION`) |
| `device_ts` | **required**, top-level, RFC 3339 UTC (`…Z`). One timestamp per payload — it is also the dedup key component (§8.3.2) |
| `is_buffered` | `true` = this payload replays readings captured while offline (§8.3.3) |
| `clock_drift` | `true` = device knows its RTC is untrusted (server flags the rows) |
| `readings[]` | one entry per reporting pack; each carries its `pack_id` |

**Diagnostics-only payloads are valid**: `readings: []` with the envelope
fields present is retained as a self-report (`GET /api/v1/gateways/{slot}/diagnostics/`)
and treated as success — use it for health reporting without fabricating data.

#### 8.3.1 Metric catalog (server-seeded; send only what your packs measure)

| `metric_key` | Unit | Alert tier-1? | Value shape |
|---|---|---|---|
| `temp` | °C | yes (velocity lane) | `{"avg", "min", "max"}` |
| `humidity` | % | no | `{"avg", "min", "max"}` |
| `lux` | lux | no | `{"avg", …}` |
| `nh3` | ppm | yes (velocity lane) | `{"avg", "min", "max", "max_ts"}` |
| `water_l` | L | no | `{"avg", …}` |
| `co` | ppm | yes | `{"avg", …}` |
| `power_mains` | bool→numeric | no | `{"avg": 0|1}` |
| `power_gen` | bool→numeric | no | `{"avg": 0|1}` |

- `avg` is what threshold alerting evaluates; `min`/`max` feed analytics.
- `max_ts` is **NH₃-only** and rides alongside `max`: it locates the peak
  within the 60 s reporting window (seconds into the window; e.g. `"max_ts":
  58` = the peak happened at second 58). The server's **velocity emergency**
  rule fires an instant Tier-1 alert — bypassing every suppression gate — when
  `max_ts` ≥ the configured threshold (default `50`, key
  `nh3_max_ts_velocity_threshold`). A late-window peak means the ammonia spiked
  mid-reading rather than drifting. Send it whenever your pack can compute it;
  omit it and only the `avg`-based rules run.
- THI (temperature-humidity index) is **derived server-side** when `temp` and
  `humidity` are co-present in the same reading — never compute or send `thi`
  yourself.
- Unknown metric keys are ignored by alerting but stored; prefer the catalog
  names exactly.

#### 8.3.2 Dedup — one payload per `device_ts`

The server dedups per (org, resolved sensor slot, `device_ts`). A redelivered
payload (same `device_ts`) is a **silent no-op**. Consequences for you:

- Bumping `device_ts` on a re-send is *mandatory* if the readings changed.
- A fully redelivered payload after a publish-timeout retry is safe — do not
  "fix" duplicates by changing timestamps on retries; just retry identically.

#### 8.3.3 `is_buffered` — offline catch-up (replay)

While offline, keep capturing readings and **keep their original
`device_ts`**. On reconnect, publish them with `is_buffered: true` (batch as
convenient within the 1 MB packet limit). Server behaviour you may rely on:

- Buffered rows land on the polite/historical lane (rate-limited per org so a
  replay storm cannot starve realtime traffic).
- Each row keeps its **original** `device_ts` — historical accuracy is
  preserved, never "now"-stamp buffered data.
- The pipeline tracks a per-gateway replay counter and drives an automatic
  replay analysis: if a buffered window crossed thresholds while you were
  offline, retroactive `REPLAY_ANALYSIS` alerts are generated server-side. You
  never raise alerts yourself — just report honestly.
- Realtime (`is_buffered: false`) is the low-latency lane: keep the normal
  cadence (`report_interval_sec`) there.

### 8.4 Status + Last Will (tenant lane)

Publish on connect (retained, QoS 1):

```json
{"gateway_slot_id": "GW-2026-0042", "status": "ONLINE"}
```

Register a Last Will on the **same topic** with `{"gateway_slot_id": "…",
"status": "OFFLINE"}` so an unclean death is visible. Values are **uppercase**
`ONLINE`/`OFFLINE`; anything else is ignored by the server. `gateway_slot_id`
must mirror the topic segment (identity rule, §4.3).

The server writes the LWT key per (org, gateway) and closes/keeps the
sensor-loss watchdog accordingly — an honest LWT is what prevents false
"sensor lost signal" alerts while you reboot.

---

## 9. QoS & delivery guarantees (summary)

- **Everything is QoS 1** in both directions. Exactly-once is *not* used;
  duplicates are handled by idempotency instead (retained-version monotonic
  guard for config; `(org, slot, device_ts)` dedup for telemetry).
- QoS 1 + persistent session (24 h) means: commands and desired documents sent
  while you are offline are delivered on reconnect within the session window.
  After 24 h offline the session expires — the retained `desired` still
  re-delivers on subscribe, so convergence never depends on the session.
- Retry publish until the PUBACK arrives; never assume fire-and-forget.

---

## 10. Edge-case matrix (we test every row)

| Case | Required device behaviour |
|---|---|
| `desired` delivered twice (retained re-delivery) | second copy = no-op (same `config_version`) |
| Newer version, then an older one | ignore the older (monotonic guard) |
| Power lost after apply, before report | on reboot retained doc re-arrives → immediately re-report `APPLIED` |
| `slot_id` in document is not yours | ignore, log fault, do not apply |
| Broker unreachable for a week | keep retrying (backoff+jitter); converge on reconnect; replay buffered telemetry with original timestamps |
| Report publish fails | retry; server republishes retained `desired` until it sees the report |
| Command arrives while applying an older config | still `ACK` → act → respond; commands are independent of config versioning |
| `ROTATE_CREDENTIAL` received | prove new password in `result`, **do not reconnect** until next natural reconnect |
| Clock RTC lost | set `clock_drift: true`, still send best-effort `device_ts`; server-side drift handling covers it |
| Telemetry for an unknown `pack_id` | (installer error) — the whole payload is dead-lettered with `UNKNOWN_SLOT`; firmware should still send, ops fixes the registry |
| MQTT 5 client | **rejected at CONNECT** by broker policy — pin 3.1.1 |
| Publish on tenant topic while unbound | refused by ACL — treat refusal as fatal misconfiguration, log and continue on device lanes |

---

## 11. Acceptance checklist (what we will run against your firmware)

1. Cold boot → connect with flash-only credentials → subscribe 4 device lanes.
2. Retained `desired` → validate → apply → `APPLIED` on `reported` (with
   persisted version).
3. Re-delivery of the same version → no-op, no report storm.
4. Version 43 then version 42 → 42 ignored.
5. Command on device lane → `ACK` → `SUCCESS` on `command/response` with
   matching `command_uuid`; expired envelope → `EXPIRED` reply; unknown action
   → `FAILED` + reason.
6. `ROTATE_CREDENTIAL` → proof in `result`, no mid-rotation reconnect, old
   session survives until commit.
7. Post-bind: verify derived tenant topics match `topics` block before first
   publish; telemetry on tenant lane with the §8.3 schema; status retained
   ONLINE + LWT OFFLINE (verified by killing the client uncleanly).
8. Unbound publish attempt on a tenant topic → broker refusal is handled
   gracefully (logged, no crash, no retry storm).
9. 10 minutes offline → catch-up with `is_buffered: true`, original
   `device_ts`, no duplicates after retry.
10. Diagnostics-only payload (`readings: []`) accepted as health report.
11. Reconnect storm simulation (50 units) → jittered backoff, no thundering herd.

---

## 12. Reference implementation

`scripts/firmware_sim.py` in the backend repository is a protocol-level device
simulator written **independently from the backend, from this document's topic
strings and payload shapes alone**. If your firmware disagrees with it, the
simulator is probably wrong — that argument is cheaper to have now than after
hardware exists. The HIL harness (`scripts/hil_handover/hil_harness.py`) runs
the same scenario against a real broker, including the unbound-publish refusal
assertion and the buffered-replay round-trip.

---

## 13. Quick reference card (print this one)

```
CONNECT   host:8883 · TLS · MQTT 3.1.1 · username=unit_id · password from flash
          keepalive 30s · clean=False · retry forever, backoff + jitter

SUBSCRIBE boot:  devices/{id}/desired (retained) · devices/{id}/command
POST-BIND ADD:   (nothing extra — tenant commands arrive on …/commands)

PUBLISH   devices/{id}/reported          config reports   (QoS 1)
          devices/{id}/command/response  command answers  (QoS 1)
          …/gateways/{slot}/telemetry    schema 2.0       (QoS 1)
          …/gateways/{slot}/status       ONLINE retained + LWT OFFLINE

ON desired: slot_id==mine? version newer? apply WHOLE doc → verify topics block
            → persist version → report APPLIED
ON command: ACK → act → SUCCESS/FAILED/EXPIRED (always answer; expires_at=UTC;
            ROTATE_CREDENTIAL: prove new password in result, don't reconnect)

TELEMETRY top-level: gateway_hardware_id, gateway_slot_id(=topic!), 
            schema_version:"2.0", device_ts(UTC,RFC3339), is_buffered,
            clock_drift, readings[{pack_id, metrics{...}}]
            metrics: temp/humidity/nh3/water_l {avg,min,max}; nh3 +max_ts;
            co/lux/power_* — THI is derived server-side, never send it

NEVER     publish before binding · trust a document addressed to another device ·
          merge config deltas · reconnect during credential rotation ·
          use MQTT 5.0 · assume LEDs/buttons in provisioning
```

*Questions → Morghdari software team. Changes to this document are versioned
in the backend repo (`docs/`); the broker config single source of truth is
`emqx/emqx.conf`.*
