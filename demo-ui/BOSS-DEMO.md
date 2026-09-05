# Boss-Mode Walkthrough: show the board, don't explain the machine

Plain-language script for demoing the v0.4 showcase to a non-technical owner.
The whole point: farms and live numbers, nothing else on screen.

> Short version (one page, what to click + what to say): **`BOSS-DEMO-SCRIPT.md`**.
> This file is the full version. Same tour, plus setup, recovery, and troubleshooting.

## Before the demo (5 minutes)

1. Start the stack:
   `bash scripts/start_demo_stack.sh`
2. Open http://127.0.0.1:8000/demo/ in the presentation browser.
3. Sign in: `demo-owner` / `demo-pass-123` (pre-filled).
   The board opens on **farms**. That is the demo screen.

## The screen (what your boss sees)

- **"Your farms, live right now"**: three real farms, Baharestan, Morvarid,
  Nahavand, each with two saloons (Saloon A / Saloon B).
- Under each farm name, the flock line, e.g. **"20,000 birds · day 21"**. The
  bird count and day of cycle come from the seeded flock data; the day counts
  up as the flock ages, so expect 21 to 23 depending on when the seed ran.
- Every saloon shows **live temperature and ammonia** ticking every 5 seconds,
  straight from the fake gateways publishing over the real MQTT pipeline.
- Top strip: **3 farms / 6 saloons / 12 sensors online** and "updated Xs ago".

## The 60-second tour (what to say)

> "These are your farms, right now. Every number on this screen is coming in
> live from a sensor. Nothing here is a mock-up. Temperature, ammonia, and
> whether every sensor is reporting."

1. **Point at the numbers ticking.** "Watch the ammonia column. It changes
   every few seconds. That's real data arriving right now."
2. **Switch to English and back** (top-right button) to show the product
   speaks both languages. The owner usually stays in Persian.
3. **Click "جزئیات" (Details) on one saloon.** The engineering view opens.
   Say: "If you ever want more, every saloon has its own page. But you don't
   need it."

## When the board turns amber (the scripted breach, ~2 min after start)

About two minutes in, a scripted ammonia spike pushes one sensor over the safe
limit. The board does three calm things:

1. A **soft amber strip appears** at the top, with the saloon named, e.g.
   "Temp over safe limit — Saloon A · Baharestan (30.4)" (exact on-screen wording)
2. The saloon's **value turns orange** and the chip reads **"Needs attention"**,
   in the owner's language, not "Tier 1 breach".
3. Nothing else changes. No pop-ups, no red flashing.

What to say: *"This is the system doing its job. One sensor in this saloon is
reading ammonia above the safe line. It flags it the moment it happens. When
the air recovers, the flag clears by itself."*

## Recovery (about 2 minutes later)

Watch the same saloon: the ammonia number drops back below the limit, the
amber strip disappears, the chip returns to **live**. Say: *"It resolved
itself. The system watched the air come back and closed it out. No one had to
do anything."*

## Don't

- Don't mention tiers, escalation, state machines, Redis, MQTT, or "the
  pipeline". If asked "how does it work?", say: *"Sensors report every minute;
  the system checks every reading against the safe limits and only flags what
  matters."*
- Don't click into EMQX or the engineering console during the tour.
- Don't show the alert inbox first. It reads as machinery. Farms first.

## Stuck?

- Board empty? The simulator must be running. Check `backend/demo-sim.log`
  (or `sim.log` if you started the simulator by hand) for
  "simulating 12 fake devices".
- Nothing ticking? Check the subscriber: `backend/subscriber.log` should show
  "connected to broker" and "subscribed". Restart with
  `bash scripts/start_demo_stack.sh --stop`, then start again.