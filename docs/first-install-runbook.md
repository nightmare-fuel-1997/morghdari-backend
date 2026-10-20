# First install — the runbook

_For the person installing the first gateway on a real farm. Every step says
what to do, who can do it, what a good result looks like, and what a bad one
means._

This is the companion to `hardware-firmware-requirements-v1.md` (what the device
must do) and `what-we-built-explained.md` (why the flow looks like this).

---

## Before you start

You need four things:

| | Why |
|---|---|
| **A registered unit** — its `unit_id`, one-time password, and QR claim code | Without it the device has no identity |
| **A manager or owner** of the customer organisation | Only they can request a gateway |
| **A platform admin or support user** | Only they can approve and assign |
| **A platform installer user**, assigned to the job | Only they can bind the device |

Two people are normally involved: someone at head office (approve + assign) and
the installer on site. The installer cannot approve their own work and cannot
assign themselves — that separation is deliberate.

**The device does not need to be switched on until step 6.** Steps 1–5 are
server-side only, so you can bind a gateway that is still in its box.

---

## Step 1 — Register the unit at the factory

*Who: hardware team. Where: factory or staging, before shipping.*

```
POST /api/v1/platform/devices/units/
{ "hardware_id": "AA:BB:CC:DD:EE:FF", "unit_id": "GW-0001", "model": "GW-4" }
```

**Good:** `201` with `unit_id`, `password`, `claim_code`.

> **The `password` and `claim_code` are shown ONCE.** They are stored only as
> hashes. If you lose them, re-issue from the unit's endpoints — you cannot
> read them back.

Print the `claim_code` as the QR label and stick it on the device. This label
is the installer's proof that they are holding *this* device.

**Bad results:**

| Response | Means |
|---|---|
| `409 unit_id already exists` | That serial is already registered. Use it, or pick another. |
| `409 hardware_id already registered` | The MAC is taken. Two units cannot share one. |
| `403` | Your account is not hardware/admin staff. |

---

## Step 2 — Request the gateway

*Who: the customer's manager or owner.*

```
POST /api/v1/gateway-requests/
{
  "farm_spec": { "name": "Shahriar Farm", "province_id": 1, "city_id": 2 },
  "expected_gateways": 2
}
```

Send `farm_spec` if the farm does not exist yet, or `farm_id` if it does —
**exactly one, never both.** `expected_gateways` is how many gateways this farm
needs; one job is created per gateway.

**Good:** `201` with the request `id`.

**Bad results:**

| Response | Means |
|---|---|
| `400 missing required keys: city_id` | The farm spec is incomplete. |
| `400 send farm_id OR farm_spec, not both` | Pick one. |
| `400 pending gateway request cap reached` | Too many open requests. Review the existing ones. |

---

## Step 3 — Approve

*Who: platform admin or support.*

```
POST /api/v1/gateway-requests/{id}/review/
{ "decision": "APPROVE" }
```

**Good:** `200` with `farm_id` and `jobs_created`.

> **Note for the app team:** this response no longer contains `password`,
> `username` or `gateway_slot_id`. Approval mints nothing any more — the device
> already has its identity from step 1. If a client reads those fields, it will
> break here and nowhere else.

**Bad results:**

| Response | Means |
|---|---|
| `409 request already reviewed` | Someone got there first. Check the jobs. |
| `404 request not found` | Wrong id, or a different organisation. |

---

## Step 4 — Assign the job to an installer

*Who: platform support.*

```
POST /api/v1/provisioning/installations/{job_id}/assign/
{ "installer_id": 42, "reason": "region north" }
```

Find the job id in `GET /api/v1/provisioning/installations/?state=PENDING`, and
the installer in `GET /api/v1/provisioning/installations/assignable/?org_id=…`
(optionally `&province_id=` to filter by region).

**Good:** `200`, job `state` becomes `ASSIGNED`.

**Bad results:**

| Response | Means |
|---|---|
| `400 installer is not an active member of this org` | Add them to the org first. |
| `409 job is COMPLETED` | Nothing to assign. |

---

## Step 5 — Bind the device (server-side; device can stay off)

*Who: the assigned installer.*

```
POST /api/v1/provisioning/installations/{job_id}/gateway/
{ "unit_id": "GW-0001", "claim_code": "the-code-from-the-label" }
```

This is the moment the device stops being a box and becomes this farm's gateway.
It creates the slot, expands the device's permissions to the farm's topics, and
consumes the claim code.

**Good:** `200` with `job.state = "BOUND"` and `gateway_slot_id` equal to the
`unit_id`.

**Bad results — these are the ones you will actually see:**

| Response | Means | What to do |
|---|---|---|
| `400 invalid claim code` | Wrong code for this unit | Re-read the label. The code is per-device, not per-order. |
| `409 claim code already consumed` | This unit is already bound | Check `job.state`. If `BOUND`, you are done — this is a replay, not an error. |
| `409 unit is already bound to another job` | The device belongs to a different farm | Find that job first. Do **not** force it. |
| `404 unit not found` | Wrong `unit_id`, or the unit is retired | Check the registration. |
| `404 job not found` | Wrong job id, or a different org | |

> **A lost or destroyed label:** support can bind without a code via
> `POST …/gateway/override/` with a mandatory `reason`. It is audited. Installers
> cannot do this — the possession proof is exactly what their role is for.

---

## Step 6 — Power the device on

Now switch it on and let it connect. Nothing else to do on the server.

**What good looks like:** within a minute or so, the slot reports
`applied_config_version` equal to `desired_config_version`, and the job's
`gateway_state_view` reads `READY`.

The device finds its configuration in the retained message on
`devices/{unit_id}/desired`. If it was switched on before the bind, it will pick
it up on the next connect — nothing needs re-sending.

**If it does not appear, check `GET /api/v1/provisioning/config-state/`:**

| State | Means | Where to look |
|---|---|---|
| `PENDING_SYNC` | The device has never been sent a configuration | The publisher could not reach the broker — check its TLS CA and credential |
| `STALLED` | Sent, but the device has not caught up | Device-side: is it connected? Does it accept the topic? |
| `FAILED` | A publish or a report failed | `last_publish_error` carries the reason |
| `READY` | Configured, waiting for the installer to submit | Go to step 9 |

---

## Step 7 — Mount a sensor (repeat per sensor)

*Who: the installer.*

```
POST /api/v1/saloons/{saloon_id}/sensor-slots/
{ "position_id": 12, "allowed_metric_keys": ["temp", "humidity"] }
```

A **slot** is where a sensor goes — a mounting position in a saloon. It is not
the sensor itself.

**Good:** `201` with a `slot_id`.

---

## Step 8 — Register the sensor against the slot

*Who: the installer.*

```
POST /api/v1/provisioning/installations/{job_id}/sensors/
{ "pack_id": "a1b2c3d4", "slot_id": "SS-…" }
```

`pack_id` is the 8-hex node id printed on the sensor. It must be exactly 8 hex
characters.

**Good:** `201` with `config_version` incremented. The device receives a new
configuration carrying the sensor, usually within seconds.

**Bad results:**

| Response | Means |
|---|---|
| `400 pack_id must be the 8-hex LoRa node id` | Typo. A wrong node id becomes a sensor that never reports, and it looks like a stalled gateway later. |
| `409 pack is already mapped to another slot` | That physical sensor is recorded somewhere else. Find it; do not force it. |
| `404 sensor slot not found in this job's farm` | The slot belongs to a different farm. |

**Photos (optional but wanted):** `POST …/gateway/photo/` and
`POST …/sensors/{slot_id}/photo/`, multipart `file`, jpeg or png, up to 10 MB.

---

## Step 9 — Submit

*Who: the installer, once the gateway is mounted and wired.*

```
POST /api/v1/provisioning/installations/{job_id}/submit/
```

**Good:** `200`. If the device has already applied its configuration, the slot
becomes `PROVISIONED` immediately and the job becomes `COMPLETED` — with **no
further approval from anyone.**

If the device has not caught up yet, the job waits at `SUBMITTED` and activates
on its own when the report arrives.

**Bad results:**

| Response | Means |
|---|---|
| `409 job is ASSIGNED; only a BOUND job can be submitted` | Bind the device first (step 5). |
| `200 already_submitted: true` | You pressed it twice. Nothing is wrong. |

---

## Done — what to verify

```
GET /api/v1/provisioning/config-state/
```

The gateway should appear with `state: "PROVISIONED"`,
`applied_config_version == desired_config_version`, and `last_publish_error: null`.

That is a working installation.

---

## The four failures worth memorising

Everything else is a variation on these.

**1. "Not authorised" / connection refused by the broker**
The device's credential is not acceptable. In order of likelihood: the unit is
`RETIRED`; the credential is inactive; the claim code was consumed by a
*different* device; the broker's database grants are missing (a deployment
problem, not a device problem).

**2. Connected, but the configuration never arrives**
The device is fine; the **server** could not publish. Look at
`last_publish_error`. Usually the publisher's TLS CA or its broker credential.
The device looks broken, and it is not.

**3. `STALLED` and stays there**
The server sent, the device did not apply. Device-side: wrong topic, wrong
protocol version, or the document failed its own validation. The device's fault
log is the place to look, not ours.

**4. Identity mismatch in the logs**
The `slot_id` inside a report disagrees with the topic it arrived on. That is
either a device firmware bug or a replay from another device's outbox. It is
logged, never silently accepted.

---

## When something is genuinely wrong

Capture these four things before asking for help — they answer almost every
question:

1. `unit_id` and `job_id`
2. The slot's row from `GET /api/v1/provisioning/config-state/` (it carries
   `last_publish_error` and `last_reported_at`)
3. The device's own log around the connect
4. Whether the device ever appears in the broker's connected-clients list
