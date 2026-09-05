# Boss Board: 3-minute demo script

One page, plain language. You talk, the board does the work.
Full troubleshooting version: `BOSS-DEMO.md`.

## Before you start (60 seconds)

1. Stack up: `bash scripts/start_demo_stack.sh`
2. Open http://127.0.0.1:8000/demo/. Sign in is pre-filled (`demo-owner`),
   just click **ورود** (Sign in).
3. You land on the farms board. That's the whole show.

## The script (what to click, what to say)

**1. The headline.** Point at the heading and the summary strip:
> "These are your farms, live right now. Three farms, six saloons, twelve
> sensors, and every one of them is reporting."

**2. The numbers are real.** Point at a temperature/ammonia value, wait for it
to change:
> "Watch the ammonia. It just ticked. That's a real reading arriving from a
> sensor right now, not a mock-up. It refreshes every five seconds."

**3. The farms have faces.** Point at a farm name and its flock line:
> "Baharestan. 20,000 birds. The day of the cycle is the number right under
> the name. That's its actual flock data, sitting under the live readings."

**4. One tap deeper.** Click **جزئیات** (Details) on any saloon, then back:
> "Every saloon has its own page if you ever want more. You won't need it.
> Everything that matters is on this screen."

**5. The language switch.** Click **English** top-right, then back to فارسی:
> "Works in both. Your team picks whichever they're comfortable with."

## When the board turns amber (the part they'll remember)

A minute or two in, the scripted ammonia spike crosses the safe limit. Don't
explain it. Just narrate what's on screen:

> "There. One sensor is reading ammonia above the safe line. Notice what the
> system did. It didn't make noise, it just quietly flagged this saloon:
> 'needs attention.' That's all. Now watch what happens next."

## Recovery (a few minutes later)

Point at the same saloon as the number drops back and the flag clears:

> "The air came back and the system closed it out by itself. Nobody had to
> touch anything. That's the whole product. It watches the barn so you don't
> have to."

## Don't say

- No "Tier 1", "breach", "escalation", "pipeline", "Redis", "MQTT".
- If asked how it works: *"Sensors report every minute; the system checks
  every reading against the safe limits and only flags what matters."*
- Don't open EMQX or the engineering console. Farms first, always.

*Tip: the flock day counts up from the seed date, so nothing needs refreshing
before the show. If you ran DB experiments, re-run `demo_farm_seed` to heal
saloon names and flocks.*