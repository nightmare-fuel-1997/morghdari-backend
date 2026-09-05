# Flutter Compat Layer — Additive Adapter Spec (Contract v1.6.0-draft, App v0.6 scope)

> **Status:** DRAFT 2026-09-05 · **Additive-only** — no `ALTER` on core tables, no change to
> existing `/api/v1` paths, no refactor of internal contracts.
> **Canonical homes:** this file (compat surface) · `API Contract (OpenAPI).yaml` v1.5.0 (frozen core)
> · `database.md` (frozen schema) · `Security Architecture.md` §4.1 (auth/RLS) · `System Walkthrough`
> §5 (WebSocket) · decisions: `Open Items Tracker.md` §4o · plan: `VERSIONS.md` §v0.6 · release draft:
> `docs/releases/v1.6-compat-draft.md` · history: `CHANGELOG.md [Unreleased → 1.6.0]`.
>
> **Source of the 16 Flutter calls:** Flutter `docs/api_calls.md` (2026-09-05):
> `GET /api/orgs/`, `GET /api/orgs/{orgSlug}`, `GET /api/orgs/{orgSlug}/farms`,
> `GET /api/orgs/{orgSlug}/devices`, `GET /api/farms/`, `GET /api/farms/{farmSlug}`,
> `GET /api/farms/{farmSlug}/devices`, `GET /api/devices/`, `GET /api/devices/{deviceSlug}`,
> `GET /api/telemetry/orgs/{orgSlug}/latest`, `GET /api/telemetry/orgs/{orgSlug}/history`,
> `GET /api/telemetry/farms/{farmSlug}/latest`, `GET /api/telemetry/farms/{farmSlug}/history`,
> `GET /api/telemetry/devices/{deviceSlug}/latest`, `GET /api/telemetry/devices/{deviceSlug}/history`,
> `WS /ws/farms/{farmSlug}/telemetry`.

---

## 1. Design constraints (non-negotiable)

1. **Zero Core Schema Mutation.** Core tables (`organizations`, `farms`, `saloons`,
   `gateway_slots`, `saloon_sensor_slots`, `saloon_telemetry`, …) are frozen per `database.md`.
   The only schema addition is ONE new table, `legacy_slug_map` (§2). No new columns on
   existing tables, no CHECK/PK/FK changes, no RLS policy edits.
2. **Zero change to the v1.5.0 surface.** All 48 existing `/api/v1` paths keep their verbatim
   paths, verbs, auth and envelopes. Compat routes live under a SEPARATE prefix so the
   `implemented == contract` CI gate (Blueprint M6) never sees them as drift.
3. **Concept mapping is explicit, not silent.** Flutter `device` == backend `saloon`
   (one device card per saloon; gateway/sensor detail stays behind existing endpoints).
   Flutter `slug` == opaque alias resolved via `legacy_slug_map`, never a renamed PK.

## 2. The single additive table: `legacy_slug_map`

```sql
-- ADDITIVE ONLY. Core tables untouched. Created by a new migration that touches
-- no existing table.
CREATE TABLE legacy_slug_map (
  slug       TEXT PRIMARY KEY,          -- e.g. 'org-01', 'farm-01', 'device-a1'
  kind       TEXT NOT NULL CHECK (kind IN ('org','farm','device')),
  org_id     UUID NOT NULL,             -- tenant scope; joins organizations(org_id)
  farm_id    INTEGER NULL,              -- set for kind IN ('farm','device')
  saloon_id  INTEGER NULL,              -- set for kind = 'device' (== Flutter device)
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON legacy_slug_map (farm_id);
CREATE INDEX ON legacy_slug_map (saloon_id);
```

- **Backfill (one-shot, idempotent):** one row per existing `organizations` / `farms` / `saloons`
  row. Suggested stable values: `org-<first-8-of-org_id>`, `farm-<farm_id>`,
  `device-<saloon_id>`. If the Flutter team already uses `farm-01`-style slugs, insert those
  exact strings instead — the table is the source of truth either way.
- **Write path:** backfill script + admin-only creation (no runtime writes from compat reads).
- **Tenancy:** every compat resolver filters by the JWT `org_id` claim (Security §4.1 branch ③)
  AFTER resolving the slug, so a guessed foreign slug yields `404`, never cross-org data.

## 3. Route table (all `GET`, all `BearerAuth`, all member-of-org)

Compat prefix: `/api/compat/v1` (recommended) with legacy aliases under `/api/...` kept for the
shipped Flutter build. Both prefixes hit the same views; the canonical documented form is the
`/api/compat/v1` one.

| # | Canonical route | Legacy alias (Flutter as-shipped) | Role | Request | Response |
|---|---|---|---|---|---|
| C1 | `GET /api/compat/v1/orgs/` | `GET /api/orgs/` | any org member (RLS-scoped list) | `Authorization: Bearer` | `200`: `[{slug, name, created_at}]` |
| C2 | `GET /api/compat/v1/orgs/{orgSlug}` | `GET /api/orgs/{orgSlug}` | member of that org | `orgSlug` `[A-Za-z0-9-]{1,64}` | `200` single org; `404 {"error":"org not found"}` |
| C3 | `GET /api/compat/v1/orgs/{orgSlug}/farms` | `GET /api/orgs/{orgSlug}/farms` | member of that org | `orgSlug` | `200`: `[{slug, name, organization_slug, created_at}]` |
| C4 | `GET /api/compat/v1/orgs/{orgSlug}/devices` | `GET /api/orgs/{orgSlug}/devices` | member of that org | `orgSlug` | `200`: device-item array (§5) |
| C5 | `GET /api/compat/v1/farms/` | `GET /api/farms/` | member (RLS list) | none | `200`: farm-item array |
| C6 | `GET /api/compat/v1/farms/{farmSlug}` | `GET /api/farms/{farmSlug}` | member of owning org | `farmSlug` | `200` single farm; `404 {"error":"farm not found"}` |
| C7 | `GET /api/compat/v1/farms/{farmSlug}/devices` | `GET /api/farms/{farmSlug}/devices` | member of owning org | `farmSlug` | `200`: device-item array of that farm |
| C8 | `GET /api/compat/v1/devices/` | `GET /api/devices/` | member (RLS list) | none | `200`: device-item array, all visible saloons |
| C9 | `GET /api/compat/v1/devices/{deviceSlug}` | `GET /api/devices/{deviceSlug}` | member of owning org | `deviceSlug` (= saloon alias) | `200` device item (§5) |
| C10 | `GET /api/compat/v1/telemetry/orgs/{orgSlug}/latest` | `GET /api/telemetry/orgs/{orgSlug}/latest` | member of that org | `orgSlug` | `200`: one flat record (§6); `404 {"error":"no telemetry"}` when empty |
| C11 | `GET /api/compat/v1/telemetry/orgs/{orgSlug}/history` | `.../history` | member of that org | `limit` (default 100, max 1000), `offset` (default 0), `from_dt`, `to_dt` (ISO-8601 or epoch-ms, optional) | `200`: flat-record array, newest first |
| C12 | `GET /api/compat/v1/telemetry/farms/{farmSlug}/latest` | `.../latest` | member of owning org | `farmSlug` | `200`: one flat record (max `server_received_ts` across the farm's saloons) |
| C13 | `GET /api/compat/v1/telemetry/farms/{farmSlug}/history` | `.../history` | member of owning org | same 4 params; Flutter production sends `limit: 24` | `200`: **`{"farm_history": [...]}`** (exact key — the chart accepts this or a raw array) |
| C14 | `GET /api/compat/v1/telemetry/devices/{deviceSlug}/latest` | `.../latest` | member of owning org | `deviceSlug` | `200`: one flat record of that saloon |
| C15 | `GET /api/compat/v1/telemetry/devices/{deviceSlug}/history` | `.../history` | member of owning org | same 4 params | `200`: flat-record array of that saloon |
| C16 | `WS /api/compat/v1/ws/farms/{farmSlug}/telemetry?token=` | `WS /ws/farms/{farmSlug}/telemetry` (legacy path kept as alias) | member of owning org (`?token=` JWT, never header on WS) | inbound `{"action":"subscribe"\|"unsubscribe","topic":"..."}` accepted, TTL-refresh only | server pushes flat-record JSON (§7); close `4003` bad token/member, `4000` over `ws:active` cap |

## 4. New modules (all new files, no edits to existing apps)

| Module | Contents |
|---|---|
| `urls_compat.py` (new) | the 15 REST routes + legacy aliases; trailing-slash tolerant (serve directly, no 301 — minimal Flutter clients don't follow auth redirects) |
| `views_compat.py` (new) | thin views: auth → `compat_resolve_*` → service → serializer; global `{"error": ...}` mapping |
| `serializers_compat.py` (new) | `OrgCompat`, `FarmCompat`, `DeviceCompat`, `FlatTelemetryRecord` — exact Flutter key names |
| `services_compat.py` (new) | `compat_resolve_org/farm/saloon` (slug → id + membership check), `compat_flatten_reading` (metrics-map → flat), history windowing (`from_dt/to_dt` → `since/until` + `limit/offset` slice) |
| `consumers_compat.py` (new) | `FarmTelemetryConsumer`: `group_add` to every `saloon_{org}_{saloon}` group of the farm; reuses the existing `telemetry.update` fan-out, translates to flat JSON before send |
| backfill script (new, ops-run) | idempotent `legacy_slug_map` seed; prints the slug table for the Flutter team |

## 5. Device-item schema (exact Flutter keys — `DevicesList.fromJson` / `DeviceData.fromJson`)

```json
{
  "slug": "device-a1",
  "name": "سالن A1",
  "status": "online",
  "firmware_version": "",
  "sensor_capabilities": ["temperature", "humidity", "lux", "nh3"],
  "last_seen_at": "2026-09-05T11:15:00Z",
  "farm_slug": "farm-01",
  "org_slug": "org-01",
  "organization_slug": "org-01",
  "created_at": "2026-08-01T10:00:00Z"
}
```

Derivation (read-only from frozen tables — no new columns):

| Flutter key | Source |
|---|---|
| `slug` | `legacy_slug_map.slug` (kind `device`) |
| `name` | `saloons` name |
| `status` | `gateway_status`: `ONLINE` → `online`, else lower-cased (`offline`, …); empty → `""` (UI shows `unknown`) |
| `firmware_version` | always `""` — no such column exists; Flutter hides the tile on empty string |
| `sensor_capabilities` | `saloon_sensor_slots.allowed_metric_keys` ∪ metric keys seen in recent telemetry, mapped to Flutter names (`temp` → `temperature`) |
| `last_seen_at` | `max(saloon_telemetry.server_received_ts)` for the saloon, ISO-8601 `Z`; `""` when none |
| `farm_slug` / `org_slug` / `organization_slug` | reverse lookup in `legacy_slug_map`; both `org_*` keys returned (Flutter reads both) |
| `created_at` | saloon/farm creation timestamp, ISO-8601 `Z` |

## 6. Flat telemetry record (exact Flutter keys — §14 of `api_calls.md`)

```json
{
  "received_at": "2026-09-05T11:15:00Z",
  "sent_ts": "2026-09-05T11:14:59Z",
  "seq": 1042,
  "is_buffered": false,
  "clock_drift_ms": 120,
  "temperature": "24.5",
  "humidity": "55",
  "lux": "15",
  "nh3": "8",
  "schema_version": "1",
  "device_slug": "device-a1"
}
```

Flattening rules (`compat_flatten_reading`):

| Flutter key | Source |
|---|---|
| `received_at` | `saloon_telemetry.server_received_ts` (ISO `Z`; epoch-ms input also accepted on the way in) |
| `sent_ts` | `saloon_telemetry.device_ts` |
| `seq` | stream/row sequence where available, else `""` (Flutter does `value?.toString()`) |
| `is_buffered` | `saloon_telemetry.is_buffered` verbatim |
| `clock_drift_ms` | `buffered_delay_mins * 60000` rounded, else `""` |
| `temperature` / `humidity` / `lux` / `nh3` | `metrics.<temp\|humidity\|lux\|nh3>.avg` → string; missing key → `""` (never `null`: all-`null` rows are dropped, partial-`null` rows render a `NULL` marker in the chart) |
| `schema_version` | payload schema version where stored, else `"1"` |
| `device_slug` | reverse lookup `saloon_id` → slug (used by the chart to filter farm history per salon) |

History windowing: `from_dt` → `since`, `to_dt` → `until` (both accept ISO-8601 `Z` or
epoch-ms); `limit` default 100 (farm-history default 24 to match the shipped chart call), max
1000; `offset` applied after newest-first ordering. Inverted window → `400 {"error":"invalid time window"}`.

## 7. Farm WebSocket shim (C16)

- **URL:** canonical `/api/compat/v1/ws/farms/{farmSlug}/telemetry?token={jwt}`; legacy
  `/ws/farms/{farmSlug}/telemetry` aliased to the same consumer. Auth is `?token=` only
  (Security §4.1: WS never uses the `Authorization` header; nginx logs `$uri` only so the
  token never hits logs).
- **Connect:** resolve `farmSlug` → farm → its `saloon_ids`; `group_add` to each
  `saloon_{org_id}_{saloon_id}` group (Walkthrough §5.1 groups). Unknown slug or non-member →
  close `4003`. Over `ws_max_connections_per_user` → close `4000`.
- **Inbound:** `{"action":"subscribe"|"unsubscribe","topic":"..."}` accepted and refreshes the
  `ws:active:{user_id}` TTL (300 s). Anything else is ignored safely (never an exception).
- **Outbound:** first message `{"type":"connection_established", ...}`, then one flat-record
  message per `telemetry.update` fan-out (translated by `compat_flatten_reading`). Buffered
  (`is_buffered=true`) and `REPLAY_ANALYSIS` rows are NEVER pushed (Walkthrough §5) — gap-fill
  stays REST: `GET …/history?from_dt={lastReceivedAt}` with dedup on `sent_ts`.
- **Core WS untouched:** `/ws/saloon/{id}/` consumer, message set and close codes unchanged.

## 8. Error contract (mirrors current standards, Security §4.1 / Blueprint M2)

- Envelope is always `{"error": "<string>"}` — the same `ErrorResponse` as
  `API Contract (OpenAPI).yaml`. Compat additionally honors the Flutter reader order
  (`message` → `error` → `detail`) by ALSO accepting those keys on the way in where relevant;
  responses always use `error`.
- Codes: `401` bad/missing token · `403 {"error":"forbidden"}` non-member or wrong role ·
  `404` unknown slug / no telemetry · `400` inverted window or bad slug charset ·
  `429` past the `5r/s` user / per-IP `limit_req` zones · WS `4003`/`4000` as above.
- Slugs are validated `[A-Za-z0-9-]{1,64}`; anything else → `404` (never `500`, never an
  empty `200` that the UI would mistake for "no data").

## 9. What this spec deliberately does NOT include

- No `POST`/`PUT`/`PATCH` compat writes (Flutter currently sends `GET` only).
- No login compat (Flutter attaches a token out-of-band; `POST /api/v1/auth/login/` stays canonical).
- No change to telemetry retention (180 d), RLS policies, MQTT topics, or alerting.
- No `farm/saloon` CRUD (Tracker §6 `farm-saloon-crud-endpoints` stays parked for Phase-2
  self-service scope).

## 10. Acceptance checklist (docs-side; code gates live in the main repo)

- [ ] `legacy_slug_map` created by an additive migration; backfill idempotent; slug table handed to Flutter.
- [ ] All 16 Flutter calls answered against staging with the exact keys above (raw array for
  lists, `{farm_history: [...]}` for C13, flat records for C10–C15).
- [ ] Salon page flow green: C9 + C14 + C7 + C13(`limit:24`) with the fixed chart axes
  (temp 20–30 °C, humidity 40–75 %).
- [ ] Error matrix green: 401/403/404/400/429 + WS 4003/4000; no `token` in nginx logs.
- [ ] `implemented == contract` gate for `/api/v1` still green (compat excluded by prefix).
- [ ] OpenAPI minor `1.5.0 → 1.6.0` ratified with the §3 table; `CHANGELOG.md [1.6.0]` finalized.
