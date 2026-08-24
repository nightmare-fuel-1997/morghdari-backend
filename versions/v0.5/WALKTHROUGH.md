# v0.5 WALKTHROUGH — the trust-boundary drill

Prereqs: v0.4 running; simulator + a second "rogue" client for negative tests.

## 1. Step 0 — request → review

FM submits a gateway request → cap enforced (5th pending → 400) → review with a
support-session user (admin/support default): APPROVED → slot + credentials exist,
status PENDING_INSTALLATION.

## 2. Zero-touch connect

Point the simulator at its slot credentials → it fetches `GET /gateways/{id}/config/`
(Basic auth) → receives farm/saloons/positions + broker settings (NO slot ids) →
publishes. Status PENDING_APPROVAL → subscriber buffers into
`telemetry:pending_approval`. Show `XLEN` growing and NO rows in Timescale.

## 3. FM approval → drain

FM approves → status PROVISIONED, audit row written, status cache invalidated,
drain dispatched → pending entries replay **in order** into the live pipeline; rows
appear. Telemetry published *before* approval was not lost.

## 4. Rejection — discard path

Repeat with a second gateway; FM rejects with reason → buffered entries discarded,
gateway's config endpoint now 403.

## 5. Suspension — broker-level lock

Suspend a provisioned gateway → EMQX auth/ACL rejects its publishes (allow-rows,
no_match deny). The rogue client with revoked credentials cannot connect at all.

## 6. Rotation — the trust handshake

Trigger ROTATE_CREDENTIAL → gateway ACKs → swap commits → old password instantly
rejected by EMQX, new one accepted. Show the pending>24h alert config.

Demo script (record): 1 → 2 → 3 → 4 → 5 → 6. The negative tests (4, 5) are the point
— trust boundaries are proven by rejection.
