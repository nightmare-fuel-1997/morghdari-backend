# v0.4 WALKTHROUGH — the alarm drill

Prereqs: v0.3 pipeline flowing; simulator able to inject crafted readings.

## 1. The emergency (velocity bypass)

Inject NH₃ with `max_ts: 57`: alert fires **<2 s** as Tier 1 push — before any
duration logic could. Watch `alert_instances` row: `source=REALTIME`,
`current_value` stamped.

## 2. The textbook breach (15 minutes exactly)

Inject temp above threshold continuously; watch the breach key TTL refresh each
minute; at exactly elapsed=900 s Tier 1 fires. Acknowledge via API → pause key appears
(TTL = configured minutes); escalation timer visibly pauses; let the pause expire →
Tier 2 SMS to engineer, then Tier 3 to manager+doctor.

## 3. Hysteresis — no flapping

Oscillate temp right at the threshold: alerts resolve only after value drops
0.5 units inside. Count rows: one alert, one resolution.

## 4. LOS — silence detected

Stop the gateway simulator: within ~60 s + 300 s grace, LOS alert to installer.
Restart Redis mid-demo → watchdog suppressed for 10 min (config) instead of a false
fleet-wide page.

## 5. Capability gating — polite unavailability

Pick a saloon whose packs lack humidity: THI rules never evaluate; the engine logs
"THI unavailable — no humidity source", and nothing false ever fires. Add a humidity
pack → THI arms itself without redeploy.

## 6. Replay sweep — history without panic

Run a replay storm batch (v0.3 simulator): afterwards `REPLAY_ANALYSIS` alert rows
exist with batch_id, none of them pushed notifications.

Demo script (record): 1 → 2 → 3 → 5 → 6 (4 optional if time). End on the MET-ALERT-
QUALITY counters you've been collecting — they go into the retrospective.
