# v0.5 — Provisioning + Zero-Touch *(M5)*

> **Theme:** the trust boundary — unapproved hardware can never inject data.
> **Risk killed:** trust boundary.
> **Maps to:** Blueprint **M5** · **Duration:** ~1–2 weeks

## What this version delivers

- **Gateway-request flow** (Step 0): FM submits → platform-side review (System Admin /
  Platform Support by default via an EXTENSIBLE platform permission — GS-BOARD), cap
  from config; approval creates slot + MQTT credentials
- Installer offline PWA sync (`POST /provisioning/sync/`, installer-role scoped):
  zone assignments, sensor mappings (hardware derived server-side), replacements —
  flips status → PENDING_APPROVAL
- FM approve/reject (+ `provisioning_audit_log`, status-cache invalidation in the same
  transaction); approve dispatches `drain_pending_approval`
- Zero-touch config endpoint: `GET /gateways/{id}/config/` Basic-auth against
  `mqtt_device_credentials` (bcrypt), rate-limited by nginx zone, org_id/farm_id in
  payload, slot_ids deliberately ABSENT (R2)
- Pending buffer semantics: PENDING_APPROVAL telemetry held in
  `telemetry:pending_approval`; REJECTED/SUSPENDED discarded; EMQX ACL allow-rows +
  no_match deny enforced per provisioning state
- **Staged credential rotation**: pending_password_hash → swap on gateway ACK
  (`ROTATE_CREDENTIAL` command envelope), bcrypt only (EMQX-native), never emailed

## Exit demo

Unapproved gateway's telemetry sits in pending · approve → drains in order · reject →
discarded · suspended gateway fails at EMQX auth · installer cannot self-approve ·
rotation ACK-commit dance end-to-end with the simulator · R3/R4/R6 suites green.

## Files

[TASKS.md](TASKS.md) · [WALKTHROUGH.md](WALKTHROUGH.md) · [TUTORIAL.md](TUTORIAL.md)

## Look-back

Did the per-slot sync lock hold under concurrent installers (409 path)? Do real
transitions match the Blueprint §11 state matrices? Did rotation's envelope
discrimination rule hold (and defer during replay)?
