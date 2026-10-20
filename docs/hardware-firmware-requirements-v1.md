# Gateway firmware requirements — bootstrap lane (v1.0)

**To:** hardware / firmware team
**From:** Morghdari software team
**Date:** 2026-10-19
**Scope:** everything the gateway must do *before* it is installed, plus the
configuration handshake that follows. Supersedes nothing; the existing
`Gateway Firmware Specification` and `MQTT Topic Map` remain authoritative for
telemetry schema 2.0 and the tenant topics.

---

## 0. The one blocking item

> **Subscribe to `devices/{your-own-unit_id}/desired` at boot, before you have
> any organization, farm, or configuration.**

That single behaviour is on the critical path for the software team, because the
whole installation flow — farm discovery, sensor assignment, configuration,
installation-complete reporting — is built on top of it.

**Nothing else in this document blocks us.** Everything else below is either
already implemented on our side, or is firmware behaviour we can validate with a
protocol simulator (see §7). If you can only give us a date for §0, give us just
that date.

---

## 1. The three identities — get these right and the rest follows

| Identity | Example | Assigned by | Stored where | Changes? |
|---|---|---|---|---|
| `unit_id` (also `slot_id`) | `GW-2026-0042` | us, at registration | device flash, broker, database | **never** |
| MQTT password | 43 random chars | us, once | device flash only, bcrypt-hashed in DB | may be rotated |
| `claim_code` | 22 random chars | us, once | **printed on the QR label** | single use |

Two rules follow, and they matter:

1. **The device already knows its own identity from the factory.** It is never
   told its `unit_id` by the installer, by a config file, or by us. This is what
   lets it connect *before* any farm exists — it needs no external information
   to start.
2. **The password never leaves the factory line.** We never print it, never ship
   it, and the installer never types it. It goes into flash once and is compared
   by the broker against a bcrypt hash. The QR code on the label carries the
   `claim_code`, which is a *one-time installation token*, not a credential.

There is a fourth identity you should know about but not manage: **`pack_id`**,
the 8-hex-digit LoRa node id of each sensor (e.g. `a1b2c3d4`). Burned at
manufacture and printed on the sensor. The installer records it in our system;
you just forward it.

---

## 2. What the factory line must do

1. Receive `unit_id`, password, and `claim_code` from us (one API call returns
   all three, exactly once).
2. Write `unit_id` + password into **encrypted flash**.
3. Print a label carrying a QR code containing `unit_id` and `claim_code`, and
   the `unit_id` in human-readable form.
4. Destroy the plaintext password from the production line's memory.

The device must power on and connect using nothing but what is in its flash.

---

## 3. What the device must do — five behaviours

### 3.1 Connect, before anything else exists

```
host: <see §5>   port: 8883   TLS required (plaintext 1883 is disabled)
username: unit_id
password:  from flash
protocol:  MQTT 3.1.1 (pinned fleet-wide)
```

Retry with exponential backoff, forever. A device that cannot reach the broker
must keep trying — not give up, and not require a reset. At this point the
device has **no organization and no farm**, and that is a valid, supported state.

### 3.2 Subscribe to exactly three topics — all built from your own id

| Topic | Direction | Notes |
|---|---|---|
| `devices/{unit_id}/desired` | we → you | **retained**; arrives immediately on subscribe |
| `devices/{unit_id}/command` | we → you | not retained; one-shot |
| `devices/{unit_id}/reported` | you → us | your config/status reports |

You have **no other permissions** in this state. Specifically you cannot publish
telemetry — there is no topic you are permitted to use. That is deliberate, not
a fault: installers bind you to a farm before telemetry is meaningful.

### 3.3 When `desired` arrives: apply it and report

1. Read `slot_id` from the document. If it does not equal your own `unit_id`,
   **ignore the document** and log a fault. Do not act on a document addressed
   to another device.
2. Read `config_version` (an integer). If it is **not greater** than the version
   you last applied, ignore the document (idempotent no-op).
3. Apply the document — see §4 for its contents.
4. Persist the applied `config_version` to flash **before** reporting.
5. Publish on `devices/{unit_id}/reported`:

```json
{"schema_version": "1.0", "slot_id": "<unit_id>", "config_version": 42,
 "state": "APPLIED", "error": null, "ts": "2026-10-19T09:15:00Z"}
```

`state` is `APPLIED` or `FAILED`. On `FAILED`, put a short human-readable reason
in `error` — it is surfaced verbatim to the operator, so it is worth writing
clearly.

### 3.4 On reconnect, converge without being asked

Re-subscribe to the three topics. Because `desired` is **retained**, the broker
re-delivers the current document immediately. If you already applied that
version, publish the `APPLIED` report again straight away. From a cold boot with
a wiped working memory this should still converge — the retained document is
sufficient on its own.

There is no "request config" message, and you never need one. If we publish a
new document while you are offline, you get it on reconnect. This is deliberate:
it removes the entire class of "command sent while the device was down" bugs.

### 3.5 After binding: switch to the tenant topics

The desired document tells you your `org_id` and `farm_id` (and repeats the full
topic set). From then on:

| Topic | Direction |
|---|---|
| `{org}/farms/{farm}/gateways/{slot}/telemetry` | you → us |
| `{org}/farms/{farm}/gateways/{slot}/status` | you → us |
| `{org}/farms/{farm}/gateways/{slot}/commands` | we → you |
| `{org}/farms/{farm}/gateways/{slot}/commands/response` | you → us |
| `…/clock/sync`, `…/ota`, `…/diagnostics/*` | both |

Status is retained: publish `{"status": "ONLINE"}` on connect and register a Last
Will with `{"status": "OFFLINE"}` on the same topic so an unclean death is
visible. The status values are **uppercase** (`ONLINE` / `OFFLINE`).

---

## 4. The configuration document is complete, never a delta

This is the single most important property for you to design around.

```json
{
  "schema_version": "1.0",
  "config_version": 42,
  "slot_id": "GW-2026-0042",
  "org_id": "9f8c2d34-...",
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
  "topics": {"telemetry": "...", "status": "...", "commands": "...",
             "commands_response": "...", "desired": "...", "reported": "...",
             "command": "..."},
  "server_ts": "2026-10-19T09:15:00Z"
}
```

Because it is published **retained**, it must be sufficient to rebuild your whole
state from scratch. Assume it may be the first and only message you ever receive.
Replace your entire sensor list, intervals, and topic set with what it says — do
not merge with what you had.

Two practical consequences:

- **It contains no secrets.** No password, no claim code. Safe to log and to
  diff between versions.
- **`topics` is included so you can verify it.** Derive the topic strings
  yourself from `org_id` / `farm_id` / `slot_id` and compare with the document. A
  mismatch means a mis-addressed document — refuse it rather than publish into a
  topic nobody reads.

---

## 5. What you must NOT assume about the hardware

This list came directly from the product owner, and it is not negotiable for
v1:

- **No LED, no buzzer, no button.** Installation is entirely backend work; the
  installer scans a QR code and the device proves nothing physically. Do not
  design a provisioning handshake that depends on an indicator.
- Do not require the installer to enter anything. No password, no serial typing
  beyond scanning what is already on the label.
- Do not assume the device is online when it is installed. Binding is paperwork
  on our side; the configuration lands whenever the device next connects.
- Do not assume you will be told the time. `server_ts` in the document is the
  reference — use it, or use clock sync.

---

## 6. Edge cases we will test, so please handle them

| Case | Required behaviour |
|---|---|
| Document arrives twice (retained re-delivery) | Second copy is a no-op (same version) |
| Newer version arrives, then an older one | Ignore the older one (monotonic guard) |
| Power lost after apply but before report | On reboot, retained doc re-delivered → immediately re-report `APPLIED` |
| `slot_id` in the document is not yours | Ignore, log a fault, do not apply |
| Broker unreachable for a week | Keep retrying; converge on reconnect with no operator action |
| Report publish fails | Retry your report; we keep republishing `desired` until we see it |

---

## 7. How you can de-risk this today, without waiting for us

A protocol-level simulator already exists in this repository
(`scripts/firmware_sim.py`) that implements exactly §3 and §4 of this document,
written independently of the backend so it can be run against our broker as a
stand-in for real hardware. It is a readable reference implementation — if your
firmware disagrees with what it does, the simulator is probably wrong, and that
is a useful argument to have early.

We are running the same scenario against a real EMQX 5.5 broker in CI
(`scripts/e2e_bootstrap_lane.py`). You can run it locally against the dev stack
too; it needs nothing but a broker and the credentials it is given.

---

## 8. Summary — your five-line contract

1. Store `unit_id` + password in encrypted flash; the QR label carries
   `unit_id` + `claim_code`.
2. On boot, connect with `username = unit_id`, retry forever.
3. Subscribe to `devices/{unit_id}/desired` and `/command`; publish to
   `devices/{unit_id}/reported`. Nothing else.
4. On `desired`: validate `slot_id`, check `config_version` is newer, apply the
   **whole** document, persist the version, report `APPLIED` on `reported`.
5. After binding, use the tenant topics the document gives you; retained LWT
   `ONLINE` / `OFFLINE` on `…/status`.
