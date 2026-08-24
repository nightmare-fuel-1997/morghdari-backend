# v0.8 WALKTHROUGH — disaster day (on purpose)

Prereqs: v0.7 production/staging live; HIL rig assembled.

## 1. PITR drill (T2)

Pick a timestamp; note a "lost" test row written after it; execute DR §3.1 verbatim;
verify the row is gone and everything else intact; record elapsed time vs RTO 4 h.

## 2. Redis catastrophe (T3)

Wipe the redis volume on staging → AOF replay path → rebuild tasks → cache warmers →
LOS suppression → inject a breach: alerts fire normally again. Total time vs RTO
15 min.

## 3. Real radio, real farm physics

Power the ESP32+LoRa gateway beside a real saloon wall: zero-touch fetch → publishes
flowing → walk the pack to the far corner watching RSSI → cut mains for 30 min →
power back → FIFO replay lands with original timestamps.

Demo script (record): all three drills, each ending with its verification query/log
line. Update DR Testing Log table — first three entries ever.
