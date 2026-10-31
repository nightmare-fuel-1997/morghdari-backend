# The MQTT Device Protocol, Explained — companion to `hardware-mqtt-protocol-v2.md`

**What this document is.** A plain-language explainer of the device protocol: what each
rule says, **why we chose it**, what we rejected, and what breaks if it is ignored.
It carries the context the specification assumes but never states.

**Who it is for.** The product owner (non-engineer), and any AI assistant asked to
explain the protocol. Paste this whole document into a chat and ask questions —
the "Questions you can ask" section at the end has ready-made prompts.

**How it relates to the spec.** `hardware-mqtt-protocol-v2.md` is the *contract* the
hardware team implements. This document is the *reasoning* behind that contract.
Where they disagree, the spec wins — but they should not disagree.

---

## 1. The context an AI needs before explaining anything

### 1.1 What the product is

Morghdari is a multi-tenant SaaS for poultry farms. A customer (an *organisation*)
owns one or more *farms*; each farm has *saloons* (poultry houses); inside a saloon,
sensors are mounted at *positions* (e.g. "North wall"), and each mounted sensor is a
*sensor slot*. The system watches temperature, humidity, ammonia (NH₃), light, water,
CO and power, raises alerts when conditions threaten the birds, and reports KPIs
(mortality, feed conversion, growth).

### 1.2 Who the players are

| Player | What it is | Who builds it |
|---|---|---|
| **Gateway** | The box on the farm wall. Has a cellular modem (a Quectel module) and a LoRa radio. Collects readings from sensor packs and talks to our server over the internet. | **The hardware team — this is what the protocol is for.** |
| **Sensor pack** | A small battery LoRa transmitter with a `pack_id`, mounted at a position. Sends readings to the gateway by radio. | The hardware team |
| **Broker (EMQX 5.5.1)** | The message post office. Devices connect to it, not to our application. It authenticates them, checks what they are allowed to do, and routes messages. | Us (already running) |
| **Subscriber** | Two identical worker processes that read messages from the broker, validate them, and hand them to the pipeline. Two for redundancy. | Us |
| **Backend (Django)** | Business logic: stores readings, computes alerts, serves the web app. | Us |
| **Database (PostgreSQL)** | The permanent record. | Us |
| **Frontend** | The web dashboard the farm staff use. | Another team |

The device never talks to the Django backend directly for telemetry. It talks to the
broker. The broker is the only door.

### 1.3 The problem the protocol solves

A gateway sits on a farm with unreliable power and unreliable mobile coverage. It must:

- work before it belongs to any farm (it is manufactured, shipped, then installed);
- survive power cuts, reboots, and days without signal without losing data or needing
  a human to press a button;
- never be trusted blindly (a stolen or misconfigured device must not be able to write
  data into someone else's farm);
- deliver readings that are **provably** the device's, with timestamps we can trust
  enough to bill and alert on;
- be updatable (thresholds, sensor list, reporting interval) without a firmware flash.

Every rule in the spec exists to satisfy one of those five sentences.

---

## 2. The big design decisions, and why

This is the section to read first. Everything else in the spec follows from these.

### 2.1 Why MQTT and not HTTPS requests?

The device sends a small reading every 60 seconds, forever, from a 4G modem. With
HTTPS each message would need its own TCP+TLS handshake — expensive in power, data and
latency — and the server could never push anything *to* the device, because the device
sits behind a carrier NAT with no inbound address.

MQTT keeps one long-lived TLS connection open. Both directions share it. The broker
queues messages for a device that is temporarily offline. This is exactly what a
battery-powered box on a farm needs.

**Rejected:** HTTP polling (device asks for config every minute) — wasteful, and the
"push a command now" case becomes impossible.

### 2.2 Why QoS 1 everywhere, and idempotency instead of "exactly once"

MQTT offers three delivery levels:

| QoS | Meaning | Cost |
|---|---|---|
| 0 | fire and forget | messages can vanish |
| 1 | "at least once" — retried until acknowledged | **duplicates are possible** |
| 2 | "exactly once" — four-step handshake | slow, and only guarantees one hop |

We use **QoS 1 everywhere** and make duplicates harmless instead of trying to prevent
them. That is the key idea: *the system is designed so that receiving the same thing
twice changes nothing.*

- For configuration: every config document carries a `config_version`; a device ignores
  any version it has already applied. Re-delivering version 42 twice is a no-op.
- For telemetry: every payload carries a `device_ts`; the server drops a second payload
  with the same `(org, sensor slot, device_ts)`. A retried publish is silently absorbed.
- For commands: each has a `command_uuid`; a repeated reply is matched to the same
  command and does not double-execute.

**Why not QoS 2?** It doubles the round-trips and still cannot give exactly-once across
our whole pipeline (broker → subscriber → queue → database). A duplicate-immune design
is cheaper, simpler on a microcontroller, and provable at every hop.

**Consequence for firmware:** when a publish times out, **retry it identically**. Do not
"fix" a suspected duplicate by changing the timestamp — that would create a genuinely
new reading and defeat the dedup.

### 2.3 Why the config is a *retained* document, not a request/response

"Retained" means the broker keeps the last message on that topic and delivers it to
**every future subscriber** immediately. We publish the device's full configuration to
`devices/{unit_id}/desired` as a retained message.

Result: a device never has to ask for its configuration. It subscribes, and the config
is simply there — after a reboot, after a week offline, after a factory reset of its
settings. There is no "config request" message, no polling, no second protocol.

**The document is always COMPLETE, never a delta.** It must be sufficient to rebuild the
device's entire state from a wiped memory. That is why the device *replaces* its sensor
list and interval with what the document says, instead of merging.

**Rejected:** incremental updates ("add sensor X", "change interval to 90") — the device
would have to track its own state and reconcile; one lost message corrupts it forever.

### 2.4 Why there is a `desired` / `reported` pair

Two separate topics, two directions:

- `desired` — what the server wants the device to be (retained, complete).
- `reported` — what the device actually is (its applied version, or a failure).

The pair is how the system knows the difference between "we told it" and "it happened".
Without `reported`, the server would have to assume the config landed — and a device
that silently failed would keep publishing with the wrong sensor list for months.

The server keeps republishing the retained document until it sees a report, so
convergence is guaranteed from either side.

### 2.5 Why device lanes and tenant lanes are separate

A gateway is manufactured and powered on **before it belongs to any farm**. It has no
organisation, no farm, no saloon. But it still needs to receive commands (factory
diagnostics, credential rotation) and to report.

So there are two topic namespaces:

- **Device lanes** — `devices/{unit_id}/…` — exist for every unit from the moment it is
  registered. No organisation involved. Only four topics.
- **Tenant lanes** — `{org}/farms/{farm}/gateways/{slot}/…` — exist only after the
  device is bound to a farm. This is where telemetry lives.

**Why not one namespace?** Because a tenant topic cannot even be *composed* for a device
that has no org. And a pre-bind device must be physically unable to write telemetry —
that is enforced by its permissions (see 2.6), not by good behaviour.

### 2.6 Why the permission list has no wildcards

The broker holds, per device, a list of the literal topics it may use. There is no
`#` or `+` on device rows, and anything not listed is **denied by default**.

That is why "an unbound device cannot publish telemetry" is a *fact*, not a policy:
there is no tenant topic in its permission list, so the broker refuses. A stolen device
cannot invent a farm's topic and inject fake readings — its credentials are pinned to its
own four device topics until an installer binds it.

**Consequence for firmware:** a refusal from the broker is a **real fault**. Never write
firmware that treats "publish refused" as normal and continues — that hides provisioning
errors forever.

### 2.7 Why the device never computes alerts or THI

The device reports **measurements**. The server decides what is dangerous.

- Thresholds are per-farm settings that change without a firmware update.
- Alert logic includes hysteresis, escalation tiers, quiet hours, correlation between
  sensors, and per-flock stages — a decade of policy that must be centrally auditable.
- THI (temperature-humidity index) is *derived* from temp + humidity by the server, so the
  formula can change for every customer at once.

A device that raised its own alerts would need a firmware release every time a customer
changed a threshold. It would also be impossible to audit after an incident.

### 2.8 Why the clock is treated as untrusted

Cheap RTCs drift and lose time when the battery dies. A device with a wrong clock would
poison the historical record — readings dated in 1970 or in the future.

The protocol therefore: requires `device_ts` (so we can dedup and order), lets the device
declare `clock_drift: true` when it knows its clock is bad, and keeps **`max_ts`** for
ammonia so a spike inside a reporting window can still be located even if the clock is
approximate.

### 2.9 Why telemetry before approval is buffered, not dropped

A farm can install a gateway before the manager has approved it in the dashboard. The
device cannot know this — it has a config, so it publishes.

The server holds those readings in a first-in-first-out lane and replays them, with their
**original timestamps**, once approval lands. The alternative — dropping them — would
mean the first hours of a new farm's history simply do not exist, which is unacceptable
for a system whose whole job is history.

### 2.10 Why the Last Will exists

MQTT lets a client register a message the **broker** will publish on its behalf if the
connection dies uncleanly (power cut, crash, cable pulled). We use it to publish
`OFFLINE` to the status topic.

Without it, the server could not distinguish "the gateway is rebooting" from "the gateway
is alive but its sensors stopped reporting" — and would raise false "sensor lost signal"
alerts every time a farmer flipped a breaker. The honest Last Will is what keeps the
sensor-loss watchdog meaningful.

### 2.11 Why MQTT 3.1.1 and not 5.0

The fleet is pinned to protocol level 4 (MQTT 3.1.1). The broker rejects MQTT 5.0 at
connect.

- The modem modules the hardware team uses have mature 3.1.1 stacks; 5.0 support varies.
- Nothing we need from 5.0 exists on the device side. Features we do use (shared
  subscriptions between our two subscriber replicas) are broker-side and work regardless.
- A pin makes divergence loud: a client that accidentally speaks 5.0 fails immediately at
  connect instead of behaving subtly differently for months.

**Note:** "MQTT 3.1.1" is the *language version*, not the broker version. The broker is
EMQX 5.5.1, a current release. Do not confuse the two numbers.

### 2.12 Why username/password and no client certificates (for now)

TLS certificates on every device mean manufacturing a key pair per unit, protecting it
for years, and revoking it — a whole PKI programme. For phase 1 we use: TLS for transport,
plus a 43-character random password burned into the device at the factory and stored
server-side only as a bcrypt hash.

The QR label carries the `unit_id` and a **one-time claim code**, deliberately *not* the
password. A photographed label therefore cannot break authentication — the claim code only
proves physical possession at install time, and it is consumed once.

Client certificates are the planned upgrade; nothing in the protocol blocks it.

---

## 3. Section-by-section companion to the spec

Read alongside `hardware-mqtt-protocol-v2.md`.

### §1 System overview
**Says:** device → broker → subscribers → database; the hardware team builds the left
side only.
**Why:** one door in (the broker) means one place to authenticate, one place to rate-limit,
one place to see every message.
**Remember:** the device never calls the web API for telemetry.

### §2 Connection specification
**Says:** TLS on 8883 (plain 1883 disabled), MQTT 3.1.1, username = unit id, password from
flash, keepalive 30 s, persistent session recommended.
**Why:** plaintext would leak readings and credentials; a persistent session lets the
broker queue commands while the device is briefly offline.
**Remember:** the device must connect using only what is in its flash.

### §2.1 Broker limits
**Says:** 1 MB packets, 10 topic levels, 50 connections/s fleet-wide, 20 messages/s per
connection, 100 KB/s, 24 h session.
**Why:** the limits protect the fleet from one misbehaving device. The 50/s connection
limit matters most: after a regional outage, 500 devices will all reconnect at once.
**Remember:** this is why reconnect needs **jitter** — random delay so the fleet does not
arrive in the same second.

### §2.2 Reconnect behaviour
**Says:** retry forever with exponential backoff (1 s → 2 s → 4 s … capped at 5 min) plus
random jitter; on every reconnect re-subscribe, re-publish status, re-report the applied
version.
**Why:** a farm device may be offline for a week. "Give up after N tries" means a truck
roll to fix software. Re-subscribing matters because a session may have expired.
**Remember:** the retained config re-arrives by itself on subscribe — that is the
"request config" mechanism.

### §3 The four identities
**Says:** `unit_id` (never changes), MQTT password (rotatable, device-only), `claim_code`
(single-use install token, on the QR only), `pack_id` (8 hex, burned into each sensor).
**Why:** each identity answers a different question — *who is this box*, *prove it*, *do
you physically have it*, *which sensor is this*. Mixing them up is how systems leak
credentials onto labels.
**Remember:** the password never appears on any label or screen.

### §4 Topic map
**Says:** four device-lane topics before binding; five tenant-lane topics after.
**Why:** see 2.5. The split is what makes "works before it belongs to a farm" and
"cannot write to a farm it does not belong to" both true at once.
**Remember:** subscribe to exactly your own topics; never use wildcards.

### §4.3 Identity enforced by the topic
**Says:** the server takes org/farm/slot from the *topic*, and the payload's
`gateway_slot_id` must equal the topic's slot segment.
**Why:** the topic cannot be forged — the broker authenticated the device and its
permissions pin its topics. So the topic is trustworthy evidence; the payload is just a
claim. Requiring them to agree catches mis-addressed firmware immediately.
**Remember:** a mismatch is a fault, not a warning.

### §5 Provisioning lifecycle
**Says:** REGISTERED → PENDING_INSTALLATION → (PENDING_APPROVAL) → PROVISIONED, with
REJECTED/SUSPENDED as operator actions. The device can connect in every state.
**Why:** the device is built, shipped and installed before it belongs to anyone. Making
connectivity depend on approval would mean a device that cannot be commissioned.
**Remember:** in PENDING_APPROVAL the device keeps publishing; the server buffers and
replays. The device does nothing special.

### §6 Trust and auth mechanics
**Says:** bcrypt passwords checked against the database at connect; per-topic allow rows;
deny by default.
**Why:** this is what makes the security claims true rather than aspirational.
**Remember:** refusal means misconfiguration — log it loudly.

### §7 The desired document
**Says:** a complete JSON document: identity, org/farm, sensor list with pack ids and
positions, reporting interval, and the full topic set. It carries no secrets.
**Why:** complete-not-delta (2.3), safe to log and diff, and the topic list lets the device
cross-check its own derivation.
**Remember:** apply the whole thing; never merge.

### §7.2 Required behaviour
**Says:** ignore a document addressed to another unit; ignore an older version; apply
fully; persist the version *before* reporting; then report. The first document after
binding may take a few seconds — that is not a failure.
**Why:** every rule here is a failure we have seen: cross-addressed documents, replay of
old versions, a device reporting "applied" before writing to flash (then losing it on
power cut), and firmware concluding "no config = broken" during the seconds between
binding and the server's first publish.
**Remember:** persist first, report second.

### §8.1 `reported`
**Says:** `APPLIED` or `FAILED`, with a human-readable error on failure.
**Why:** operators read that string. A vague error costs a site visit.
**Remember:** retry the report; the server keeps republishing the config until it sees it.

### §8.2 Commands — the three-event protocol
**Says:** every command has a `command_uuid` and an `expires_at` (UTC). The device answers
`ACK` (received), then acts, then `SUCCESS`/`FAILED`. Answer expired commands too. Unknown
actions get a `FAILED` reply. `ROTATE_CREDENTIAL` returns the new password in the result
and must not trigger an immediate reconnect.
**Why:** three separate events exist so an operator can tell "the device is alive and
working on it" from "the device is dead" — silence is the worst possible answer. The
rotation dance exists because the broker still holds the *old* password until the new one
is proven; reconnecting early locks the device out.
**Remember:** `expires_at` is UTC. Treating it as local time once made every command look
3.5 hours expired.

### §8.3 Telemetry schema 2.0
**Says:** one payload per reporting interval, one entry per sensor pack, each metric as
`{avg, min, max}`; `device_ts` is required and is the dedup key; `is_buffered` marks
catch-up data; `clock_drift` admits a bad clock.
**Why:** `avg` drives alerting, `min`/`max` feed analytics, `device_ts` makes retries
safe, `is_buffered` lets a week of offline data arrive without pretending it is live.
**Remember:** never change `device_ts` on a retry; never fabricate readings — an empty
`readings: []` is a valid "I am alive, nothing to report".

### §8.3.1 Metric catalog
**Says:** fixed metric names and units; `max_ts` (ammonia only) locates the peak inside the
window; THI is derived server-side.
**Why:** free-form metric names make alerting impossible. `max_ts` exists because ammonia
spikes are the dangerous case — a peak at second 58 of a 60-second window means the
ventilation failed mid-interval, and the server raises an instant emergency alert instead
of waiting for the average to rise.
**Remember:** send `max_ts` if you can compute it; never send `thi`.

### §8.3.2 Dedup
**Says:** the server drops a second payload with the same `(org, slot, device_ts)`.
**Why:** see 2.2 — this is what makes QoS 1 safe.
**Remember:** identical retry = good; new timestamp on a retry = duplicate data.

### §8.3.3 Buffered replay
**Says:** while offline, keep readings with their original timestamps; publish them with
`is_buffered: true` on reconnect; they land on a rate-limited historical lane and can
generate retroactive alerts.
**Why:** the farm still needs to know that the ammonia was high while the network was down.
Rate-limiting the replay protects live traffic from a flood.
**Remember:** keep original timestamps; batch within 1 MB.

### §8.4 Status and Last Will
**Says:** publish retained `ONLINE` on connect; register a Last Will publishing `OFFLINE`
on the same topic; values uppercase; `gateway_slot_id` mirrors the topic.
**Why:** see 2.10 — the honest Last Will is what prevents false sensor-loss alerts.
**Remember:** the broker publishes the Last Will, not the device.

### §9 QoS summary
**Says:** QoS 1 both ways; duplicates handled by idempotency; persistent session 24 h.
**Why:** see 2.2.
**Remember:** retry until acknowledged; never assume fire-and-forget.

### §10 Edge-case matrix
**Says:** the twelve situations we will test: duplicate documents, older versions after
newer, power loss mid-apply, foreign documents, a week offline, failed reports, commands
during config apply, credential rotation, clock loss, unknown pack ids, MQTT 5 clients,
publishing while unbound.
**Why:** these are not hypotheticals — each one happened during development or is a
predictable consequence of the design.
**Remember:** this table is the acceptance test. A firmware that fails a row is not done.

### §11 Acceptance checklist
**Says:** the concrete tests we will run against the real board.
**Why:** "it works on my desk" is not evidence. The checklist is the definition of done.

### §12–13 Reference implementation and quick card
**Says:** where to look for a working example, and the one-page summary.
**Why:** a working example settles arguments faster than prose.

---

## 4. The words that confuse people

| Term | Plain meaning | Analogy |
|---|---|---|
| **Broker** | The server that receives and forwards messages | A post office |
| **Topic** | The address a message is sent to | A street address |
| **Retained message** | The broker keeps the last message on a topic and gives it to every new subscriber | A pinned notice on a door |
| **QoS 1** | Delivery is retried until acknowledged; duplicates possible | Registered mail, may arrive twice |
| **Last Will** | A message the broker sends on the device's behalf if it dies uncleanly | An out-of-office note posted automatically |
| **ACL** | The list of topics a device may use | A guest list at the door |
| **Dedup** | Recognising "we already have this reading" and ignoring the copy | A receipt checker |
| **Idempotent** | Doing it twice has the same effect as doing it once | Pressing a lift button twice |
| **Monotonic** | Only ever moving forward (version numbers) | A page number you never go back from |
| **Session** | The broker's memory of a device between connections | A held table reservation |
| **Desired / reported** | What we want it to be / what it says it is | Order vs delivery note |
| **Provisioning** | The process of turning a factory box into a farm device | Registering a new phone |
| **Slot / pack** | A mounted sensor position / the physical sensor | A parking space / the car in it |
| **THI** | Temperature-humidity index — a combined heat-stress number | "Feels like" for chickens |

---

## 5. What we deliberately do NOT do

| Not doing | Why |
|---|---|
| Client certificates (phase 1) | Needs a full PKI per device; username/password + TLS is adequate for now |
| Exactly-once delivery (QoS 2) | Cannot be guaranteed end-to-end anyway; idempotency is cheaper and provable |
| MQTT 5.0 | No device-side benefit; pinning 3.1.1 makes divergence loud |
| Device-side alerts | Thresholds change per customer; logic must be centrally auditable |
| Device-side THI | Derived value; formula must change without firmware updates |
| Wildcard subscriptions | Would let a device listen to other farms; permissions pin literal topics |
| Sending config as deltas | One lost message corrupts device state forever |
| Dropping telemetry before approval | The first hours of a farm's history would vanish |
| Trusting the device clock | Cheap RTCs drift; timestamps are dedup keys and billing evidence |

---

## 6. Questions you can ask an AI (copy-paste)

Paste this document, then ask any of these. The AI has enough context to answer without
guessing.

1. "Explain section 2.2 (QoS 1 + idempotency) to me as if I have never heard of MQTT.
   Use a concrete example of a duplicate reading."
2. "Why is the config document retained instead of the device asking for it? What would
   break if we removed the retention?"
3. "Walk me through what happens, step by step, from the moment a gateway is powered on
   in the factory to the moment its first telemetry row is stored in the database."
4. "What exactly happens if the gateway loses power in the middle of applying a new
   configuration? Which rules protect us?"
5. "Explain the difference between device lanes and tenant lanes, and why an unbound
   device literally cannot publish telemetry."
6. "Why does the protocol have three command events (ACK, then SUCCESS/FAILED) instead of
   one reply?"
7. "What is a Last Will, and why does it prevent false sensor-loss alerts?"
8. "Explain `max_ts` for ammonia and why a late-window peak triggers an emergency alert."
9. "What are the risks of a device treating a refused publish as normal? Give a concrete
   failure scenario."
10. "Summarise the edge-case matrix (§10) as a list of firmware requirements a developer
    could tick off."
11. "If I were the hardware team, which three rules would you tell me to get right first,
    and why those three?"
12. "Explain why the device must not compute THI or raise alerts, using an example of a
    customer changing a threshold."
13. "What does 'idempotent' mean here, and which parts of the protocol rely on it?"
14. "Where could a duplicate message cause a real-world problem, and which rule prevents
    it?"
15. "Explain the credential rotation dance (§8.2 rule 5) and what happens if the device
    reconnects too early."

---

## 7. One-paragraph summary for an AI

> Morghdari is a multi-tenant poultry-monitoring SaaS. A battery-powered gateway on a farm
> collects sensor readings over LoRa and sends them to an EMQX 5.5.1 MQTT broker over
> cellular TLS. The protocol is designed around an unreliable device on an unreliable
> network: MQTT 3.1.1 with QoS 1 everywhere, duplicates made harmless by version numbers
> and timestamp-based deduplication rather than by expensive exactly-once delivery; the
> device's configuration delivered as a retained, complete document the device applies
> wholesale; a desired/reported pair so the server can distinguish "we told it" from "it
> happened"; separate pre-bind device topics and post-bind tenant topics, with per-topic
> permissions and no wildcards so an unbound or stolen device physically cannot write into
> a farm; telemetry buffered and replayed with original timestamps when approval or
> connectivity is missing; a Last Will so an unclean death is visible; and alerts, THI and
> thresholds computed exclusively server-side so policy can change without firmware
> releases. The document the hardware team implements is `hardware-mqtt-protocol-v2.md`;
> this companion explains why each rule exists.
