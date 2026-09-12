# Hardware Handover Harness (M8 — software side of the seam)

The ESP32+LoRa gateway is the HARDWARE TEAM's deliverable; this harness is the
SOFTWARE side's executable acceptance for it. Their "done" is defined as:

```bash
python scripts/hil_handover/hil_harness.py --target real <credential flags>
# → exit 0 (no FAIL; SKIP is not allowed on a real device)
```

Run it against the simulator TODAY to prove the software side of every probe:

```bash
python scripts/hil_handover/hil_harness.py --target sim \
  --base-url https://localhost --slot FARM-01-GW-A \
  --org <org-uuid> --farm-id 1 --saloon-id <id> \
  --manager-token <manager-jwt> \
  --mqtt-host localhost --mqtt-port 1883 \
  --mqtt-user <dev-mqtt-user> --mqtt-pass <dev-mqtt-password> \
  --insecure
```

## Probes → firmware contract

| Probe | Proves (firmware-spec) | Receiver-side evidence |
|---|---|---|
| P1 config_fetch | §2.1 boot ladder — zero-touch GET /config/ | HTTP 200 + R2 body (org/farm ids, MQTT endpoint) |
| P2 telemetry_visible | §2.2 aggregation path, schema 2.0 | row in `GET /saloons/{id}/telemetry/` with the SAME `device_ts`, `is_buffered=false` |
| P3 lwt_offline | §2.5 keep-alive→LWT coupling | status topic published 'offline' ≤ keepalive after abrupt socket death |
| P4 buffered_replay | §2.4 offline FIFO + dual timestamps | buffered rows keep ORIGINAL `device_ts` + `is_buffered=true` (HIL-1/4) |
| P5 ota_ack | §2.6 OTA T6→T7 | device answers on `…/ota/response` (SUCCESS/FAILED) |
| P6 rotation_ack | §2.7 commands + staged rotation | device ACKs ROTATE_CREDENTIAL on `…/commands/response` |

`--target sim` SKIPs P5/P6 honestly — no device exists to answer. When the
hardware team brings the rig, P5/P6 SKIP**s are FAILs: a real gateway must
respond.

## The handover rule (why this unblocks the backend)

1. Run `--target sim` now → the software side is proven green.
2. `scripts/check_hil_readiness.py` at the same time → the backend HIL
   readiness (config route, hardware/time, O9, T5–T9 ACLs, OTA tooling).
3. Hardware bring-up day → run `--target real`. Failures with a clear
   probe + evidence are **hardware-team findings** (HIL-1..14 evidence
   table in the v0.8 testing doc), not a backend blame-swap.

## Environment variables (flags win over env)

`HIL_BASE_URL` `HIL_SLOT` `HIL_HARDWARE_ID` `HIL_ORG` `HIL_FARM_ID` `HIL_SALOON_ID`
`HIL_MANAGER_TOKEN` `HIL_DEVICE_USER` `HIL_DEVICE_PASSWORD`
`MQTT_HOST` `MQTT_PORT` `MQTT_USERNAME` `MQTT_PASSWORD` `MQTT_TLS` `MQTT_TLS_INSECURE`

`HIL_HARDWARE_ID` is the `gateway_hardware_id` stamped in probe payloads
(default `{slot}-HW`). It must match a provisioned `gateway_sensor_mapping`
row or the pipeline dead-letters `UNKNOWN_SLOT` — that rejection IS the probe
working, not a harness bug.

## Prereqs

- paho-mqtt (the backend venv has it), broker + API reachable
  (`docker compose -p morghdari … up -d` — but NEVER against production creds;
  use the dev/rehearsal EMQX and a scratch org/dev gateway slot).
- The simulator's `build_payload` is reused for honest schema-2.0 payloads
  (`scripts/simulate_gateway.py` is import-safe).