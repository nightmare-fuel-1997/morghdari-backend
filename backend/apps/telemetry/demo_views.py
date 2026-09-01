"""DEMO-ONLY pipeline introspection — never exposed in production.

Mounted under /demo/api/ inside the SAME gate as the /demo/ page
(DEBUG or MORGHDARI_DEMO_UI=1). Requires an authenticated JWT so the manager
must be signed in on the console to press anything.

Gives the browser eyes on the machinery: stream lengths, consumer-group lag,
dead-letter tail, and a one-click publish that drives the REAL pipeline
(handle_message -> drain) so card-by-card the flow is visibly live.
Redis access DELEGATES to consumers._redis() so test modules can inject an
isolated logical DB with a single patch.
فارسی: نقاطِ دیدِ زنده برای دمو — فقط با گیتِ مخصوص دمو و توکن معتبر.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from django.db.models import Max
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.alerts.models import AlertInstance, LifecycleTemplate, LifecycleThresholdRule
from apps.alerts.services.redis_keys import (
    breach_key,
    pack_lwt_key,
    sensor_status_key,
)
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization
from apps.telemetry.models import FlockCycle, SaloonTelemetry
from morghdari.subscriber.pipeline import (
    STREAM_HISTORICAL,
    STREAM_REALTIME,
    dedup_key,
    handle_message,
)

from apps.telemetry.consumers import (
    CONSUMER_GROUP,
    _redis,
    drain_stream,
)

DLQ_STREAM = "telemetry:dead_letter"


def _group_snapshot(client, stream):
    """Pending/lag for our consumer group, tolerating missing group/stream."""
    try:
        for group in client.xinfo_groups(stream):
            if group.get("name") == CONSUMER_GROUP:
                return {
                    "pending": group.get("pending", 0),
                    "last_delivered_id": group.get("last-delivered-id"),
                    "lag": group.get("lag"),
                }
    except Exception:
        pass  # stream/group not created yet — zero state
    return {"pending": 0, "last_delivered_id": None, "lag": None}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pipeline_status(request):
    client = _redis()
    return Response({
        "streams": {
            "realtime": client.xlen(STREAM_REALTIME),
            "historical": client.xlen(STREAM_HISTORICAL),
            "dead_letter": client.xlen(DLQ_STREAM),
        },
        "groups": {
            "realtime": _group_snapshot(client, STREAM_REALTIME),
            "historical": _group_snapshot(client, STREAM_HISTORICAL),
        },
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dlq_tail(request):
    client = _redis()
    try:
        count = max(1, min(int(request.query_params.get("count", 5)), 20))
    except ValueError:
        count = 5
    entries = []
    for entry_id, fields in client.xrevrange(DLQ_STREAM, count=count):
        entries.append({
            "id": entry_id,
            "topic": fields.get("topic", ""),
            "reason": fields.get("reason", ""),
            "payload_preview": fields.get("payload", "")[:200],
        })
    return Response({"count": client.xlen(DLQ_STREAM), "entries": entries})


@api_view(["GET"])
@permission_classes([AllowAny])
def demo_identity(request):
    """Public barn identity for the login backdrop (demo-gated route).

    No JWT on purpose: the /demo/ landing shows WHICH barn you are about to
    sign into. Counts come from the real seeded world (farms, saloons) and
    the live Redis status keys (sensors online) — nothing hardcoded, so the
    numbers always agree with the board behind the same login.
    فارسی: شناسنامهٔ دمو برای پشت صحنهٔ ورود؛ اعداد از دادهٔ واقعی و کلیدهای زنده، نه عدد ثابت.
    """
    org = Organization.objects.filter(name="Demo Farm").first()
    if org is None:
        return Response(
            {"error": "Demo Farm org not found — run the seed first"},
            status=status.HTTP_404_NOT_FOUND,
        )

    # "Farms" = farms that actually hold saloons (the board groups the same
    # way); the org may keep shell farms (absorbed legacy, demo-pipe) that
    # would overstate the barn identity.
    saloons = Saloon.objects.filter(org=org)
    farms = saloons.values("farm_id").distinct().count()
    saloon_count = saloons.count()
    # One pipelined mget for liveness — same contract as farm_topology.
    slots = list(
        SaloonSensorSlot.objects.filter(org=org)
        .order_by("saloon_id", "slot_id")
        .values_list("saloon_id", "slot_id")
    )
    client = _redis()
    alive = client.mget([sensor_status_key(s, sid) for s, sid in slots]) or []
    return Response({
        "farms": farms,
        "saloons": saloon_count,
        "sensors_online": sum(1 for v in alive if v is not None),
    })


def _claims_org(request):
    """Caller's org uuid from validated JWT claims (never from the body)."""
    claims = getattr(request, "jwt_claims", None)
    if not claims:
        auth = getattr(request, "auth", None)
        claims = getattr(auth, "payload", {}) if auth is not None else {}
    return claims.get("org_id")


def _ensure_mapping_for_org(org):
    """Idempotent slot+mapping so publishes have somewhere to land."""
    existing = (
        GatewaySensorMapping.objects.filter(org=org, pack_id="P-A")
        .select_related("slot__saloon__farm")
        .first()
    )
    if existing is not None:
        return existing

    tag = uuid.uuid4().hex[:8]
    province = Province.objects.create(name=f"demo-pipe-P")
    city = City.objects.create(province=province, name=f"demo-pipe-C")
    farm = Farm.objects.create(org=org, name=f"demo-pipe-F", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"demo-pipe-S")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="demo")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-demo-{tag}",
        saloon=saloon,
        org=org,
        position=position,
    )
    return GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"demo-hw-{tag}", pack_id="P-A", slot=slot, org=org
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def simulate_reading(request):
    """Drive the REAL pipeline once for the CALLER'S org.

    Org comes from the validated JWT claim (TUTORIAL §3.1), never from the
    body — a forged body org would be an identity fault downstream anyway.
    Reports honestly which of three outcomes happened: published /
    duplicate-dropped / dead-lettered (with reason).
    فارسی: سازمان از ادعای توکن خوانده می‌شود نه از بدنه؛ نتیجه صادقانه اعلام می‌شود.
    """
    mapping = _ensure_mapping_for_org(_claims_org(request))
    if mapping is None:
        return Response(
            {"error": "Demo Farm org not found — run the seed first"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    buffered = bool(request.data.get("buffered"))
    # "dangerous": NH3 spike with max_ts=59 → the velocity lane fires INSTANT
    # Tier-1 (bypasses the 30 s demo duration gate) so a manager can watch an
    # emergency alert appear in seconds, exactly as a real lethal spike would.
    dangerous = bool(request.data.get("dangerous"))
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if buffered:  # step back an hour so buffered windows are visibly OLD
        now -= timedelta(hours=1)
    device_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    org_id = str(mapping.org_id)
    gateway_slot_topic = mapping.slot.slot_id

    key = dedup_key(org_id, gateway_slot_topic, device_ts)
    was_duplicate = _redis().exists(key) == 1

    if dangerous:
        nh3_metrics = {"avg": 58.0, "min": 40.0, "max": 62.0, "max_ts": 59}
    else:
        nh3_metrics = {"avg": round(12 + (now.minute % 5), 1),
                       "min": 10.0, "max": 20.0, "max_ts": (now.minute * 7) % 60}

    payload = json.dumps({
        "gateway_hardware_id": "demo-hw",
        # MUST mirror the topic segment exactly (identity gate cross-checks):
        "gateway_slot_id": gateway_slot_topic,
        "schema_version": "2.0",
        "device_ts": device_ts,
        "is_buffered": buffered,
        "readings": [{"pack_id": "P-A",
                      "metrics": {
                          "temp": {"avg": round(26 + (now.minute % 7) * 0.4, 1)},
                          "nh3": nh3_metrics,
                      }}],
    }).encode()

    # Topic contract: /farms/{farm_id}/ must be the REAL farm of the slot —
    # the pipeline carries this segment into the stream entry untouched.
    farm_id = mapping.slot.saloon.farm_id
    topic = f"{mapping.org_id}/farms/{farm_id}/gateways/{gateway_slot_topic}/telemetry"
    result = handle_message(_redis(), topic, payload, db_fetch=lambda *a: mapping.slot_id)

    lane = STREAM_HISTORICAL if buffered else STREAM_REALTIME
    drained = drain_stream(lane, count=50)

    outcome = "duplicate-dropped" if was_duplicate else "published"
    if result is not None:  # rejected by a gate → surfaced instead of a 500
        entries = _redis().xrevrange(DLQ_STREAM, count=1)
        reason = entries[0][1].get("reason") if entries else "unknown"
        outcome = f"dead-lettered ({reason})"

    return Response({
        "outcome": outcome,
        "lane": lane.split(":")[-1],
        "device_ts": device_ts,
        "dedup_key_tail": key[-24:],
        "drained_from_lane": drained,
        "note": (
            "dedup guard absorbed this publish — effectively-once, by design"
            if was_duplicate else
            "entry enriched + lane-drained through the REAL pipeline"
        ),
    })


def _alert_state(client, saloon_ids, breach_duration_sec):
    """Live v0.4 state-machine snapshot for the dashboard's escalation timers.

    For every open alert (active_alert JSON in Redis) and every accumulating
    breach key, reports elapsed seconds against the duration gate so the page
    can render a live "Tier 2 in mm:ss" countdown from the SAME keys the
    engine uses. Read-only; never mutates state.
    """
    import json as _json
    import time as _time

    now = _time.time()
    out = {}

    # All breach keys for the org's saloons in ONE pipelined round-trip.
    _metrics = ("temp", "nh3", "humidity", "thi")
    _keys = [breach_key(sid, m) for sid in saloon_ids for m in _metrics]
    _raw_by_key = dict(zip(_keys, client.mget(_keys) or []))

    # All open alerts for those saloons in ONE query (org-scoped by RLS).
    _rows = (
        AlertInstance.objects
        .filter(saloon_id__in=saloon_ids, resolved_at__isnull=True)
        .exclude(source="REPLAY_ANALYSIS")
        .exclude(metric_type="los")
        .values("saloon_id", "id", "metric_type", "current_status",
                "highest_tier_reached", "current_value", "triggered_at")
    )
    _by_saloon: dict = {}
    for r in _rows:
        _by_saloon.setdefault(r["saloon_id"], []).append(r)

    for sid in saloon_ids:
        entry = {"breaches": [], "active_alerts": []}
        for metric in _metrics:
            raw = _raw_by_key.get(breach_key(sid, metric))
            if not raw:
                continue
            try:
                state = _json.loads(raw)
            except ValueError:
                continue
            started = state.get("started_at")
            if not started:
                continue
            elapsed = max(0.0, now - float(started))
            entry["breaches"].append({
                "metric": metric,
                "value": state.get("value"),
                "elapsed_sec": round(elapsed, 1),
                "remaining_sec": round(max(0.0, breach_duration_sec - elapsed), 1),
                "alerted": bool(state.get("alerted")),
            })
        for r in _by_saloon.get(sid, []):
            entry["active_alerts"].append({
                "id": str(r["id"]),
                "metric": r["metric_type"],
                "status": r["current_status"],
                "tier": r["highest_tier_reached"] or 1,
                "value": r["current_value"],
                "elapsed_sec": round(max(0.0, (_dj_now() - r["triggered_at"]).total_seconds()), 1),
            })
        if entry["breaches"] or entry["active_alerts"]:
            out[str(sid)] = entry
    return out


def _dj_now():
    from django.utils import timezone as dj_tz
    return dj_tz.now()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def sensor_off(request):
    """DEMO-ONLY: simulate a sensor outage by deleting the live sensor:status
    keys for ONE slot of the caller's org. The next LOS watchdog sweep (60 s)
    sees the expired status and raises the Tier-1 'sensor offline' alert —
    the same path a real dead sensor takes. No direct alert insertion; the
    demo presses the same button reality does.
    فارسی: حذفِ کلیدهای زندهٔ حسگر تا پایشگرِ LOS همان مسیرِ قطعیِ واقعی را برود."""
    from apps.alerts.services.redis_keys import sensor_status_key as _status_key

    org = _claims_org(request)
    if org is None:
        return Response({"error": "no org claim"}, status=status.HTTP_400_BAD_REQUEST)

    slot_id = request.data.get("slot_id")
    saloons = Saloon.objects.filter(org=org)
    if slot_id:
        slots = SaloonSensorSlot.objects.filter(org=org, slot_id=slot_id)
    else:
        # No slot chosen: take the first slot of the first saloon (stable order).
        slots = SaloonSensorSlot.objects.filter(
            org=org, saloon=saloons.order_by("id").first()
        ).order_by("slot_id")[:1]
    if not slots:
        return Response({"error": "no slots found for org"}, status=status.HTTP_400_BAD_REQUEST)

    client = _redis()
    cleared = []
    for slot in slots:
        key = _status_key(slot.saloon_id, slot.slot_id)
        client.delete(key)
        cleared.append({"saloon_id": slot.saloon_id, "slot_id": slot.slot_id})

    return Response({
        "outcome": "sensor-off",
        "cleared": cleared,
        "note": "status keys deleted — LOS watchdog raises the offline alert on its next sweep",
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def acknowledge_alert_view(request):
    """DEMO-ONLY acknowledgement — the SAME mechanism the REST endpoint will
    use in v0.6 (apps.alerts.services.ack): one audit row + one escalation
    pause key, atomically. The alert must belong to the caller's org.
    فارسی: رسیده‌سازی با همان سازوکارِ M4-06؛ ردیف و کلیدِ توقف با هم."""
    from apps.alerts.services.ack import acknowledge_alert

    org = _claims_org(request)
    alert_id = request.data.get("alert_id")
    if not alert_id:
        return Response({"error": "alert_id required"}, status=status.HTTP_400_BAD_REQUEST)

    alert = (
        AlertInstance.objects
        .filter(id=str(alert_id), resolved_at__isnull=True)
        .first()
    )
    if alert is None or str(alert.org_id) != str(org):
        return Response({"error": "alert not found"}, status=status.HTTP_404_NOT_FOUND)

    ack = acknowledge_alert(alert, request.user, notes="demo acknowledgement")
    return Response({
        "outcome": "acknowledged",
        "alert_id": str(alert.id),
        "pause_minutes": ack.pause_minutes,
    })


def _flock_cycle_days():
    """Cycle length for the flock dial: the max rule day of the default
    lifecycle template (the seed's template spans day 1..45). 45 is also the
    Ross standard for a world with no template — an honest fallback, never a
    hardcoded UI constant. Single aggregate — no template round-trip first."""
    top = LifecycleThresholdRule.objects.filter(
        template__org__isnull=True, template__is_default=True
    ).aggregate(m=Max("day_to"))["m"]
    return top or 45


def _flock_payload(flock, now, cycle_days):
    """Boss-board flock summary: breed, bird count, day of cycle, cycle length
    (for the flock dial). Day 1 = the placement day; None when no open flock
    exists. cycle_days is resolved ONCE per request by the caller — a lookup
    per saloon here would be an N+1 on every 5-second board poll."""
    if flock is None:
        return None
    day = max(1, (now - flock.start_date).days + 1)
    return {
        "breed_type": flock.breed_type,
        "placement_count": flock.placement_count,
        "day": day,
        "cycle_days": cycle_days,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farm_topology(request):
    """Live-demo farm topology + per-slot liveness + latest reading — for the
    /demo/dashboard/ page. Caller's org ONLY (claims, never the body).

    Liveness comes from the SAME Redis keys the LOS watchdog uses
    (sensor:status:{saloon}:{slot}, TTL = sensor_status_ttl_sec): a slot that
    has not refreshed within the grace window is reported stale — the same
    honesty the alert engine sees. The latest reading is the real
    saloon_telemetry row for that slot (RLS-scoped), so numbers on the page
    are exactly what the API would return.
    فارسی: نقشهٔ توپولوژی مزرعهٔ نمایشی + وضعیت زندهٔ هر حسگر + آخرین خوانش.
    """
    org = _claims_org(request)
    if org is None:
        return Response({"saloons": []}, status=status.HTTP_200_OK)

    saloons = Saloon.objects.filter(org=org).select_related("farm").order_by("id")
    out_saloons = []
    latest_by_slot: dict[str, dict] = {}

    for row in (
        SaloonTelemetry.objects.filter(saloon__org=org)
        .order_by("slot_id", "-server_received_ts")
        .distinct("slot_id")
        .values("slot_id", "device_ts", "metrics", "is_buffered", "server_received_ts")
    ):
        latest_by_slot[row["slot_id"]] = row

    client = _redis()

    from apps.system_config.models import SystemConfiguration
    # All config keys in ONE query (config-is-data; the demo seed tunes the
    # escalation pacing to 1/2 min so the countdowns move live).
    _cfg = dict(
        SystemConfiguration.objects
        .filter(key__in=[
            "breach_duration_sec",
            "escalation_tier2_min",
            "escalation_tier3_min",
            "alert_acknowledgement_pause_minutes",
        ])
        .values_list("key", "value")
    )
    try:
        breach_duration_sec = int(_cfg.get("breach_duration_sec") or 900)
    except (TypeError, ValueError):
        breach_duration_sec = 900

    # Escalation pacing for the dashboard's countdowns — the SAME keys the
    # sweep reads every pass (config-is-data; demo seed tunes them to 1/2 min).
    def _cfg_int(key: str, default: int) -> int:
        try:
            return int(_cfg.get(key) or default)
        except (TypeError, ValueError):
            return default

    tier2_min = _cfg_int("escalation_tier2_min", 15)
    tier3_min = _cfg_int("escalation_tier3_min", 30)
    pause_min = _cfg_int("alert_acknowledgement_pause_minutes", 10)

    saloon_ids = [s.pk for s in saloons]
    alert_state = _alert_state(client, saloon_ids, breach_duration_sec)

    # Open flock per saloon for the boss board's flock summary line.
    from django.utils import timezone as dj_tz
    flock_by_saloon: dict = {}
    for flock in (
        FlockCycle.objects.filter(org=org, end_date__isnull=True)
        .order_by("-start_date")
        .select_related("saloon")
    ):
        flock_by_saloon.setdefault(flock.saloon_id, flock)

    # ── one-time, org-batched lookups (the board polls every 5 s — any
    #    per-saloon/per-slot query here is a silent N+1 on every poll) ──
    slots_by_saloon: dict[int, list] = {}
    for slot in SaloonSensorSlot.objects.filter(org=org, saloon__org=org).order_by("slot_id"):
        slots_by_saloon.setdefault(slot.saloon_id, []).append(slot)

    positions_by_saloon: dict[int, list] = {}
    for pos in SaloonPosition.objects.filter(org=org, saloon__org=org).order_by("id"):
        positions_by_saloon.setdefault(pos.saloon_id, []).append(pos)

    mapping_by_slot: dict[str, GatewaySensorMapping] = {}
    for m in GatewaySensorMapping.objects.filter(org=org, slot__saloon__org=org):
        mapping_by_slot[m.slot_id] = m

    # Liveness for every slot in ONE pipelined round-trip, from the same keys
    # the LOS watchdog uses (mget: missing key → None → offline), PLUS the
    # pack-LWT keys (retained 'offline' after a pack dies — surfaced
    # immediately, no waiting for the sensor-status TTL to lapse).
    _status_keys: list[str] = []
    _lwt_keys: list[str] = []
    _status_slots: list[str] = []
    for saloon in saloons:
        for slot in slots_by_saloon.get(saloon.pk, []):
            _status_keys.append(sensor_status_key(saloon.pk, slot.slot_id))
            _lwt_keys.append(pack_lwt_key(saloon.pk, slot.slot_id))
            _status_slots.append(slot.slot_id)
    _values = client.mget(_status_keys + _lwt_keys) or []
    _n = len(_status_keys)
    _online_values, _lwt_values = _values[:_n], _values[_n:]
    online_by_slot: dict[str, bool] = {
        slot_id: v is not None
        for slot_id, v in zip(_status_slots, _online_values)
    }
    lwt_by_slot: dict[str, str | None] = {
        slot_id: v for slot_id, v in zip(_status_slots, _lwt_values)
    }

    cycle_days = _flock_cycle_days()  # once per request, not per saloon

    for saloon in saloons:
        slots_out = []
        for slot in slots_by_saloon.get(saloon.pk, []):
            mapping = mapping_by_slot.get(slot.slot_id)
            latest = latest_by_slot.get(slot.slot_id)
            # pack_alive = the pack never said offline (last known LWT state;
            # absent key = no death signal = alive). It is independent of
            # `online` (data freshness): a SILENT pack is still alive, a
            # DEAD pack flips immediately via its will.
            _lwt = lwt_by_slot.get(slot.slot_id)
            slots_out.append({
                "slot_id": slot.slot_id,
                "pack_id": mapping.pack_id if mapping else None,
                "gateway_hardware_id": mapping.gateway_hardware_id if mapping else None,
                "online": online_by_slot.get(slot.slot_id, False),
                "lwt": _lwt,
                "pack_alive": _lwt != "offline",
                "last_device_ts": latest["device_ts"] if latest else None,
                "last_buffered": latest["is_buffered"] if latest else False,
                "last_metrics": latest["metrics"] if latest else None,
            })
        out_saloons.append({
            "id": saloon.pk,
            "name": saloon.name,
            "farm_id": saloon.farm_id,
            "farm_name": saloon.farm.name if saloon.farm else None,
            "flock": _flock_payload(flock_by_saloon.get(saloon.pk), dj_tz.now(), cycle_days),
            "positions": [p.label for p in positions_by_saloon.get(saloon.pk, [])],
            "slots": slots_out,
        })

    # Group saloons under their farm for the boss board. Farms with no saloons
    # are omitted (e.g. an emptied legacy farm after a converged re-seed).
    farms_map: dict = {}
    for s in out_saloons:
        key = s["farm_id"]
        group = farms_map.setdefault(key, {
            "id": key,
            "name": s["farm_name"] or "—",
            "saloon_count": 0,
            "online_slots": 0,
            "dead_packs": 0,
            "saloons": [],
        })
        group["saloon_count"] += 1
        group["online_slots"] += sum(1 for sl in s["slots"] if sl["online"])
        group["dead_packs"] += sum(1 for sl in s["slots"] if not sl["pack_alive"])
        group["saloons"].append(s)
    farms = sorted(farms_map.values(), key=lambda g: (g["id"] is None, g["id"] or 0))

    return Response({
        "org_id": str(org),
        "farms": farms,
        "saloons": out_saloons,
        "alert_state": alert_state,
        "breach_duration_sec": breach_duration_sec,
        "escalation": {
            "tier2_min": tier2_min,
            "tier3_min": tier3_min,
            "ack_pause_min": pause_min,
        },
    })


# ---------------------------------------------------------------------------
# v0.4 demo dashboard: per-saloon sensor inventory, window statistics, and
# EMQX reachability. Same gates and honesty rules as farm_topology: the
# caller's org only (JWT claims, never the body), liveness from the SAME
# Redis keys the LOS watchdog uses. Read-only; never mutates state.
# ---------------------------------------------------------------------------

# Seeded lifecycle thresholds used to label a metric breaching on the page.
DEMO_THRESHOLDS = {"nh3": 25.0, "temp": 30.0}


def _metric_breach(metric, value):
    limit = DEMO_THRESHOLDS.get(metric)
    if limit is None or value is None:
        return False
    return value >= limit


def _saloon_or_404(request, saloon_id):
    org = _claims_org(request)
    if org is None:
        return None, Response({"error": "no org claim"}, status=status.HTTP_400_BAD_REQUEST)
    saloon = Saloon.objects.filter(org=org, pk=saloon_id).first()
    if saloon is None:
        return None, Response({"error": "saloon not found"}, status=status.HTTP_404_NOT_FOUND)
    return (org, saloon), None


def _stats_over_rows(rows):
    """Per-metric avg/min/max/count over telemetry rows (Python-side so
    Postgres and SQLite behave identically)."""
    acc = {}
    for row in rows:
        metrics = row.get("metrics") or {}
        for metric, payload in metrics.items():
            if not isinstance(payload, dict):
                continue
            value = payload.get("avg")
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            bucket = acc.setdefault(metric, {"sum": 0.0, "count": 0,
                                             "min": value, "max": value})
            bucket["sum"] += value
            bucket["count"] += 1
            bucket["min"] = min(bucket["min"], value)
            bucket["max"] = max(bucket["max"], value)
    out = {}
    for metric, bucket in acc.items():
        count = bucket["count"]
        out[metric] = {
            "avg": round(bucket["sum"] / count, 2) if count else None,
            "min": round(bucket["min"], 2),
            "max": round(bucket["max"], 2),
            "count": count,
        }
    return out


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_sensors(request, saloon_id: int):
    """Full sensor inventory for ONE saloon of the caller's org.

    Health per slot: online (status key alive), stale (recent reading but
    status key expired), offline (no status key and no recent reading), plus
    a per-metric breach flag from the latest real row.
    """
    pair, error = _saloon_or_404(request, saloon_id)
    if error is not None:
        return error
    org, saloon = pair

    client = _redis()
    latest_by_slot = {}
    for row in (
        SaloonTelemetry.objects.filter(saloon=saloon, saloon__org=org)
        .order_by("slot_id", "-server_received_ts")
        .distinct("slot_id")
        .values("slot_id", "device_ts", "metrics", "is_buffered", "server_received_ts")
    ):
        latest_by_slot[row["slot_id"]] = row

    from django.utils import timezone as dj_tz
    now = dj_tz.now()

    # Batched per-request lookups (same contract as farm_topology): one
    # mappings query, one pipelined liveness mget — never per-slot queries.
    slots = list(
        SaloonSensorSlot.objects.filter(saloon=saloon, org=org).order_by("slot_id")
    )
    mapping_by_slot: dict = {}
    for m in (
        GatewaySensorMapping.objects.filter(org=org, slot__saloon=saloon)
        .order_by("id")
    ):
        mapping_by_slot.setdefault(m.slot_id, m)
    _alive = client.mget(
        [sensor_status_key(saloon.pk, s.slot_id) for s in slots]
    ) or []
    online_by_slot = {
        s.slot_id: v is not None for s, v in zip(slots, _alive)
    }

    sensors = []
    for slot in slots:
        mapping = mapping_by_slot.get(slot.slot_id)
        online = online_by_slot.get(slot.slot_id, False)
        latest = latest_by_slot.get(slot.slot_id)
        last_metrics = latest["metrics"] if latest else None
        breaching = []
        if last_metrics:
            for metric, payload in last_metrics.items():
                if isinstance(payload, dict) and _metric_breach(metric, payload.get("avg")):
                    breaching.append(metric)
        if online:
            health = "online"
        elif latest is not None:
            health = "stale"
        else:
            health = "offline"
        age_min = None
        if latest is not None and latest.get("server_received_ts"):
            age_min = round(max(0.0, (now - latest["server_received_ts"]).total_seconds() / 60.0), 1)
        sensors.append({
            "slot_id": slot.slot_id,
            "pack_id": mapping.pack_id if mapping else None,
            "gateway_hardware_id": mapping.gateway_hardware_id if mapping else None,
            "health": health,
            "online": online,
            "breaching": breaching,
            "last_device_ts": latest["device_ts"] if latest else None,
            "last_buffered": latest["is_buffered"] if latest else False,
            "last_metrics": last_metrics,
            "last_seen_age_min": age_min,
        })

    counts = {"online": 0, "stale": 0, "offline": 0}
    for sensor in sensors:
        counts[sensor["health"]] = counts.get(sensor["health"], 0) + 1

    return Response({
        "org_id": str(org),
        "saloon_id": saloon.pk,
        "saloon_name": saloon.name,
        "sensor_count": len(sensors),
        "health_counts": counts,
        "sensors": sensors,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_stats(request, saloon_id: int):
    """Window statistics for ONE saloon, optionally narrowed to one slot.

    ?window=24h (default 1h) bounds device_ts; ?slot_id narrows to one sensor
    so the individual sensor page can show its own averages.
    """
    pair, error = _saloon_or_404(request, saloon_id)
    if error is not None:
        return error
    org, saloon = pair

    window = request.query_params.get("window", "1h")
    hours = {"1h": 1.0, "6h": 6.0, "24h": 24.0, "7d": 168.0}.get(window)
    if hours is None:
        return Response({"error": "window must be one of 1h, 6h, 24h, 7d"},
                        status=status.HTTP_400_BAD_REQUEST)

    from datetime import timedelta
    from django.utils import timezone as dj_tz
    since = dj_tz.now() - timedelta(hours=hours)

    queryset = SaloonTelemetry.objects.filter(
        saloon=saloon, saloon__org=org, is_buffered=False, device_ts__gte=since,
    )
    slot_id = request.query_params.get("slot_id")
    if slot_id:
        queryset = queryset.filter(slot_id=slot_id)

    rows = list(queryset.order_by("device_ts").values("slot_id", "device_ts", "metrics"))
    stats = _stats_over_rows(rows)

    # Per-slot breakdown so the saloon page can compare sensors side by side.
    by_slot = {}
    for row in rows:
        by_slot.setdefault(row["slot_id"], []).append(row)
    per_slot = {slot: _stats_over_rows(slot_rows) for slot, slot_rows in by_slot.items()}

    return Response({
        "org_id": str(org),
        "saloon_id": saloon.pk,
        "saloon_name": saloon.name,
        "window": window,
        "slot_id": slot_id,
        "reading_count": len(rows),
        "stats": stats,
        "per_slot": per_slot,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def emqx_status(request):
    """Broker reachability for the demo EMQX page: TCP on the MQTT listener
    and HTTP on the dashboard API. Read-only probes, short timeouts, honest
    result - the page renders whatever it finds."""
    import os as _os
    import socket
    from urllib.request import urlopen
    from urllib.error import URLError

    host = _os.environ.get("MQTT_HOST", "127.0.0.1")
    mqtt_port = int(_os.environ.get("MQTT_PORT", "1883"))
    dash_port = int(_os.environ.get("EMQX_DASHBOARD_PORT", "18083"))

    def _tcp(port):
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except OSError:
            return False

    def _http(port):
        try:
            with urlopen(f"http://{host}:{port}/api/v5/status", timeout=2.0) as resp:
                return resp.status == 200
        except (URLError, OSError):
            return False

    return Response({
        "host": host,
        "mqtt_port": mqtt_port,
        "dashboard_port": dash_port,
        "mqtt_reachable": _tcp(mqtt_port),
        "dashboard_reachable": _http(dash_port),
        "dashboard_url": f"http://{host}:{dash_port}/",
    })
