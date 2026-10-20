# What we built, explained without jargon

_For a non-technical reader. Every term in the engineering report is defined
here in plain language, with what it does for the business._

---

## The one-paragraph version

Before this work, installing a gateway on a farm required a person at head
office to manually approve it, create a record for it, and generate a password
that someone had to carry to the farm. That password was never held by the
actual device — so every approval left behind a "ghost" record of a gateway
that did not exist. We replaced that with a flow where the physical device
already exists before it reaches the farm, the installer proves they are holding
it by scanning a code on its label, and the system switches it on automatically
once it confirms it has received its settings. No head-office approval step, no
passwords carried by hand, no ghost records.

---

## The cast of characters

It helps to know who and what exists in the system, because several of the
technical terms refer to them.

**The customer organisation ("org").** A poultry company using our platform.
Each customer can only ever see their own farms.

**The farm, the saloon, the gateway, the sensor.** A farm contains saloons
(buildings). A gateway is the box that collects readings. Sensors are the small
devices that measure temperature, humidity and so on.

**Our own staff.** Three kinds:
- **Hardware team** — registers devices at the factory.
- **Support desk** — handles customer problems, assigns work.
- **Installers** — the people who physically go to farms and mount equipment.

None of these people work for a customer. They work for us. This sounds obvious
but it caused a real problem, explained below under "org-less".

**The device unit.** A gateway as it exists *before* it belongs to anyone — a
box on a shelf at the factory with a serial number. Previously the system had no
concept of this: a gateway could only exist once it was attached to a farm. That
was backwards, and it is the root of most of what we changed.

---

## The five things you asked about

### 1. Scoped session tokens

**What it is.** A temporary digital pass. When an installer arrives at a farm,
their phone receives a pass that is valid for 15 minutes and opens exactly one
farm's records — the farm they were sent to. Nothing else.

**What it does.** It is what allows an installer to do their job at all.
Installers do not work for the customer, so normally the system has no reason to
let them see any customer's data. The pass is the exception: narrow, short-lived,
and tied to one specific job.

**Why you care.** Consider the alternative. If installers could see "everything
for the customer they are working with", then an installer sent to Farm A could
also browse Farm B, C and D — every building, every reading, every flock. If a
phone is lost, or an installer becomes an ex-installer, the exposure is the whole
customer relationship. With a scoped pass, the exposure is one farm for fifteen
minutes.

There is a second, subtler benefit. Support staff have their own, much broader
pass. We deliberately kept the two types separate so an installer can never
accidentally inherit a support operator's powers. That separation is tested
automatically, so a future code change cannot quietly merge them.

### 2. System actor

**What it is.** A named "user" in the records that represents the software
itself, rather than a person.

**What it does.** Some decisions are made by the machine, not by anyone at a
desk. For example: "this gateway has confirmed it received its settings, so mark
it as installed." That decision needs to be written down as having happened.
Without a system actor, the record would say "nobody did this", or worse, it
would be credited to whichever human happened to be logged in at the time.

**Why you care.** When a customer asks "who approved this gateway going live?",
the honest answer is "the system did, automatically, because the device confirmed
its configuration at 14:32". That is a good answer, and it is defensible. The bad
alternative is a record that credits a support agent who was on holiday that day.
It also means we can prove that no human bypassed the checks — because if a human
had done it, their name would be there instead.

### 3. Audit ledger (audit trail)

**What it is.** A permanent list of who did what, when, and to which record. It
is append-only: entries can be added but never edited or deleted.

**What it does.** Every meaningful action writes a line: an approval, an
assignment, a device binding, a photo upload, a settings change. We keep two
ledgers:
- a **company-wide** one for actions by our own staff (who do not belong to any
  customer, so they cannot be filed under a customer), and
- a **per-customer** one, so each customer's own records show their own history.

**Why you care.** Three concrete situations:
1. **Disputes.** A customer says "we never approved this" or "nobody told us the
   sensor was moved". The ledger settles it with a timestamp and a name.
2. **Investigations.** If a device is misconfigured, the ledger shows every
   change made to it and by whom, in order. Without it you are guessing.
3. **Compliance and enterprise sales.** "Can you show us an audit trail?" is a
   standard question from larger customers and from any security review. Today
   the answer is yes.

### 4. "Org-less" (and why our own staff broke the system)

**What it is.** "Org-less" simply means "does not belong to a customer company".
Our hardware team, support desk and installers are all org-less.

**What it does — and what went wrong.** The original system was built on the
assumption that every user belongs to a customer. That is true for farm managers
and vets, and false for our own staff. When an org-less person used the system,
the security rules found no customer to scope them to, so they saw **nothing** —
not an error, just empty screens. A support engineer opening the queue would have
seen a blank list and concluded there was no work, when in reality there were
dozens of pending installations.

**Why you care.** This is the single most dangerous kind of bug: it fails
silently and looks like "no data" rather than "broken". We found it, fixed it,
and — more importantly — added a test that runs against a real database so it
cannot come back unnoticed. Our ordinary automated tests could not catch it,
because they run on a simplified database that has no security rules at all.

### 5. Row-level security, and "tenant isolation"

**What it is.** Protection built into the database itself, not just the
application. Each customer's rows are tagged with their identity, and the
database refuses to return rows belonging to anyone else — even if the
application code asks for them.

**What it does.** It is a second lock. Normally we rely on the application to add
"and only this customer's data" to every query. That works until someone writes
one query and forgets. Row-level security means forgetting does not leak data;
the database simply returns nothing.

**Why you care.** This is the difference between "we try to keep customers
separate" and "the database will not let us mix them". For a multi-tenant product
sold to competing poultry companies, that is the difference between a sale and a
failed security review. It also means a single coding mistake cannot become a
data breach headline.

---

## The rest of the terms, briefly

**Claim code.** A one-time secret printed on the QR label stuck to each device.
The installer scans it to prove they are physically holding that device. It works
once and then is dead. Crucially, it is *not* the device's password — so someone
who photographs a label cannot use the device, and a lost label does not lock
anyone out.

**Retained message / desired state.** The server leaves a note in the device's
"mailbox" that stays there until collected. When the device is switched on —
even days later, even in a farm with no signal at install time — it reads the
note and configures itself. This is why an installer can bind a gateway while it
is still in its box.

**Config version.** A simple counter ("this is settings version 3"). It stops the
device from re-applying old settings it already has, and stops it from ignoring
genuinely new ones. Without it, a device that reconnects can get confused about
which instructions are current.

**Convergence sweep.** A background job that runs every minute and re-sends
settings to any device that has not confirmed them. If a message was lost, or a
device was offline, it fixes itself with nobody clicking anything. It is the
safety net that means "the installer's phone died mid-install" is an
inconvenience rather than a support ticket.

**Four-state view.** A support screen with four buckets: *never sent*, *sent but
not confirmed*, *failed (with the reason)*, and *ready*. When a farm calls and
says "our gateway shows nothing", support can answer in seconds instead of
starting an investigation.

**Installation job.** A work order — "one gateway to install at Farm X". When a
customer requests two gateways, the system creates two jobs automatically. Jobs
are what get assigned to installers and tracked to completion.

**Sensor slot vs. pack id.** A **slot** is a place where a sensor goes (a
mounting point in a saloon). A **pack id** is the serial number of the physical
sensor. Joining the two is what tells the gateway "watch this spot, with this
particular sensor". Keeping them separate means a sensor can be replaced without
losing the history of that spot.

**Provisioned.** The end state: device mounted, configured, confirmed, and
reporting. Reaching it no longer requires anyone at head office to approve
anything.

**Idempotent.** A technical word for "pressing the button twice does not create
two of something". It matters because installers work on poor mobile connections
and press Send again when nothing appears to happen.

**Coverage (88.96%).** The share of the code that the automated tests actually
exercise. It is a rough proxy for "how much of this is checked by a machine
rather than by hope". Our floor is 80%.

**OpenAPI contract.** A written description of every connection point between
our system and the mobile app. It is checked automatically against the real code,
so the app team and the server team cannot silently drift apart and discover it
in front of a customer.

**MQTT 3.1.1.** The technical language the devices speak. The hardware team can
only support this one version, so we have locked it at both ends: our software
uses it, and the message broker now actively *refuses* anything newer. That
prevents a future change from quietly breaking every device in the field.

---

## What we found along the way

The features are the visible part. These are the defects the work uncovered,
described in business terms:

| What was wrong | What it would have cost |
|---|---|
| Approving a request created a gateway record and password for a device that did not exist | Every approval left a ghost. Customers would be billed or supported for equipment they never received |
| Our own staff saw empty screens instead of their work queue | Support would tell customers "there is nothing pending" while dozens of installations sat waiting |
| Creating a saloon through the system had never actually worked | Nobody had tried it. The first customer to attempt it would have hit a hard error in production |
| Adding a sensor updated the record but did not send it to the device | An installer would wait a minute, assume it failed, and re-do the work |
| The automated tests were being run against an out-of-date copy of the code | Test results were briefly reporting success for code that was not actually being tested |

The last two are worth a sentence each. The sensor one is a user-experience
failure that looks like a device fault. The out-of-date-copy one is more
serious in principle: it means a green test result can be a lie, so we now verify
the copy before trusting the number.

---

## What is left

One thing, and it is a verification rather than new work.

All of our testing so far used a simplified message broker configuration. The
real deployment uses a different, more capable configuration — the one that lets
a brand-new device authenticate before it belongs to any customer. That
configuration has been checked in isolation and it starts up correctly, but **no
real device has ever connected through it.**

Before this goes near a customer farm, we should run one complete installation
against a copy of the production setup, using a simulated device. That closes the
last gap between "we believe it works" and "we have watched it work".

---

## If your manager asks one question

They will probably ask: **"Is it safe to put in front of a customer?"**

The honest answer today: the logic is proven and heavily tested, and the
remaining risk is concentrated in one place — the production broker
configuration, which has been validated but not yet exercised by a live device.
That is a single rehearsal run away from being closed, and it should be done
before the first real installation.
