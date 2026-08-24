# v0.7 WALKTHROUGH — production birth

Prereqs: v0.6 green; VPS access; DNS A records for api+mqtt names.

## 1. Birth

Run the TLS runbook → `docker compose up -d` → all 9 containers (subscriber-02 ships
in the base topology) healthy →
`curl https://api.morghdari.com/api/v1/health/` returns 200 with version field =
your tag.

## 2. Eyes

SSH-tunnel Grafana: dashboards populate (containers, redis, postgres, emqx,
subscriber tier). Deliberately kill subscriber-01: within minutes the
`subscriber_up < 2` CRITICAL page fires (02 absorbs traffic meanwhile).

## 3. Chaos + rollback

Deploy tag N, break something trivially, `MORGHDARI_TAG=<prev> docker compose up -d`
→ healthy again in one command. DLQ spike drill: flood malformed payloads → DLQ-depth
alert fires → trim.

## 4. Trust on production

Support engineer (with MFA) opens an audited session on the test org, suspends a
gateway per runbook → audit trail shows SUPPORT_ORG_ENTER + SUPPORT_OP_* with
incident id. Superuser password never left the vault.

## 5. Sleep-with-confidence checklist

Backup ran last night AND restored in the drill · prober green 24 h · pages tested ·
rollback rehearsed · disk trend vs capacity model noted (AU-DISK-MATH).

Demo script (record): 1 → 2 → 3 → 4, closing on 5's checklist.
