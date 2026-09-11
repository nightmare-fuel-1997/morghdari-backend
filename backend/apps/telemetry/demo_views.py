"""DEMO-ONLY telemetry introspection and live-dashboard endpoints.

Mounted under /demo/api/ inside the SAME gate as the /demo/ page (DEBUG or
MORGHDARI_DEMO_UI=1) — the production image never serves them. Every endpoint
except identity requires an authenticated JWT so the manager must be signed in
on the console to press anything.

Endpoints:
- pipeline_status / dlq_tail: live machinery eyes (stream lengths, consumer
  lag, dead-letter tail) — driven by demo-simulate's real pipeline publish.
- demo_identity: public login-backdrop counts (farms/saloons/sensors-online)
  for the seeded "Demo Farm" org — numbers come from the DB plus live Redis
  status keys, never hardcoded.
- farm_topology: the boss-board poll (saloons + slots + flock dials + pack
  liveness + dead packs). BATCHED by contract: two Redis mget pipelines per
  poll and zero per-slot key calls; SQL flat in org size (test_query_budget).
- saloon_sensors / saloon_stats: per-saloon sensor inventory and rolling
  window statistics against the telemetry hypertable.
- emqx_status: EMQX reachability shape for the connectivity card.
- simulate_reading: one-click publish that drives the REAL pipeline
  (handle_message -> drain) so the flow is visibly live.

Redis access DELEGATES to consumers._redis() so test modules can inject an
isolated logical DB with a single patch.

فارسی: نقاط دید زنده برای دمو — فقط با گیت مخصوص دمو و توکن معتبر؛ پولِ
تابلو با دو mget دسته‌ای و SQLِ مستقل از اندازهٔ سازمان.
"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from django.db.models import Max
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.alerts.services.redis_keys import (
    pack_lwt_key,
    sensor_status_key,
    thresholds_hash,
)
from apps.devices.models import (
    GatewaySensorMapping,
    SaloonPosition,
    SaloonSensorSlot,
)
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry.consumers import CONSUMER_GROUP, _redis, drain_stream
from apps.telemetry.models import FlockCycle, SaloonTelemetry
from apps.tenants.models import Organization
from morghdari.subscriber.pipeline import (
    STREAM_HISTORICAL,
    STREAM_REALTIME,
    dedup_key,
    handle_message,
)

DLQ_STREAM = "telemetry:dead_letter"
TEHRAN = ZoneInfo("Asia/Tehran")

# Demo-default threshold bands — the SAME bands demo_farm_seed writes into the
# global default template (RULE_BANDS). The dashboard breach flags read the
# real saloon:{id}:thresholds hash; when a test/scratch world never ran the
# seed, these bands keep the card honest instead of showing nothing.
DEMO_DEFAULT_MAX = {"temp": 30.0, "humidity": 75.0, "nh3": 25.0}
FALLBACK_CYCLE_DAYS = 45  # industry-standard broiler cycle when no template


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


@extend_schema(exclude=True)
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


@extend_schema(exclude=True)
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


@extend_schema(exclude=True)
@api_view(["GET"])
@permission_classes([])
def demo_identity(request):
    """Public counts for the login backdrop — real DB + live Redis status."""
    org = Organization.objects.filter(name="Demo Farm").first()
    if org is None:
        return Response({"error": "demo seed not found"}, status=404)
    farms = Farm.objects.filter(org=org).count()
    saloons = Saloon.objects.filter(org=org).count()
    slots = SaloonSensorSlot.objects.filter(org=org)
    client = _redis()
    online = sum(
        bool(client.exists(sensor_status_key(slot.saloon_id, slot.slot_id)))
        for slot in slots
    )
    return Response({"farms": farms, "saloons": saloons, "sensors_online": online})


def _claims_org(request):
    """Caller's org uuid from validated JWT claims (never from the body)."""
    claims = getattr(request, "jwt_claims", None)
    if not claims:
        auth = getattr(request, "auth", None)
        claims = getattr(auth, "payload", {}) if auth is not None else {}
    return claims.get("org_id")


# ---------------------------------------------------------------------------
# Boss board: /demo/api/farm/
# ---------------------------------------------------------------------------

def _resolve_cycle_days(org_id) -> int:
    """Span of the applicable lifecycle template, or the industry fallback
    (45 d broiler) when no template exists (test worlds, pre-seed DBs).
    Resolution chain: org default → global default — evaluated ONCE per poll
    so a larger org never costs extra statements."""
    from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule

    template = (
        LifecycleTemplate.objects.filter(org_id=org_id, is_default=True).first()
        or LifecycleTemplate.objects.filter(org__isnull=True, is_default=True).first()
    )
    if template is None:
        return FALLBACK_CYCLE_DAYS
    span = LifecycleThresholdRule.objects.filter(template=template).aggregate(
        top=Max("day_to")
    )["top"]
    return span or FALLBACK_CYCLE_DAYS


def _flock_out(flock, cycle_days: int):
    """Flock dial fields. day is 1-based (placement day = day 1) in Tehran
    local time; cycle span comes from the applied template with the industry
    fallback when none exists."""
    now_local = datetime.now(tz=timezone.utc).astimezone(TEHRAN)
    start_local = flock.start_date.astimezone(TEHRAN)
    age_days = (now_local.date() - start_local.date()).days
    return {
        "breed_type": flock.breed_type,
        "placement_count": flock.placement_count,
        "day": age_days + 1,
        "cycle_days": cycle_days,
    }


@extend_schema(exclude=True)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def farm_topology(request):
    """Boss-board poll: org-scoped saloons/slots with live liveness, latest
    metrics and pack LWT state. Two Redis mget pipelines, flat SQL."""
    org_id = _claims_org(request)
    if not org_id:
        return Response({"error": "organization context required"}, status=400)

    client = _redis()
    saloons = list(
        Saloon.objects.filter(org_id=org_id)
        .select_related("farm")
        .order_by("id")
    )
    slots = list(SaloonSensorSlot.objects.filter(org_id=org_id).order_by("slot_id"))
    slot_ids = [s.slot_id for s in slots]
    saloon_ids = [s.pk for s in saloons]

    # Batch mappings so each slot's pack + hardware id costs zero SQL.
    mapping_by_slot = {}
    for m in GatewaySensorMapping.objects.filter(org_id=org_id).select_related("slot"):
        mapping_by_slot.setdefault(m.slot_id, m)

    # Latest telemetry per slot in ONE query (rows sorted per slot; first wins).
    latest_by_slot = {}
    if slot_ids:
        for row in (
            SaloonTelemetry.objects.filter(slot_id__in=slot_ids)
            .order_by("slot_id", "-device_ts")
            .values("slot_id", "device_ts", "metrics")
        ):
            latest_by_slot.setdefault(row["slot_id"], row)

    # Open flock per saloon in ONE query (first per saloon = newest start);
    # the cycle dial span resolves ONCE for the whole poll.
    flock_by_saloon = {}
    cycle_days = _resolve_cycle_days(org_id)
    if saloon_ids:
        for flock in (
            FlockCycle.objects.filter(saloon_id__in=saloon_ids, end_date__isnull=True)
            .order_by("saloon_id", "-start_date")
        ):
            flock_by_saloon.setdefault(flock.saloon_id, flock)

    # Exactly TWO Redis round-trips: status mget + LWT mget. Never per-key.
    status_vals = (
        client.mget([sensor_status_key(s.saloon_id, s.slot_id) for s in slots])
        if slots else []
    )
    lwt_vals = (
        client.mget([pack_lwt_key(s.saloon_id, s.slot_id) for s in slots])
        if slots else []
    )

    online_keys = {
        slot_id for slot_id, v in zip(slot_ids, status_vals) if v is not None
    }
    lwt_by_slot = {
        slot_id: v for slot_id, v in zip(slot_ids, lwt_vals) if v is not None
    }

    saloon_out = []
    farm_groups = {}
    for saloon in saloons:
        flock = flock_by_saloon.get(saloon.pk)
        slot_out = []
        for slot in slots:
            if slot.saloon_id != saloon.pk:
                continue
            mapping = mapping_by_slot.get(slot.slot_id)
            latest = latest_by_slot.get(slot.slot_id)
            slot_out.append({
                "slot_id": slot.slot_id,
                "pack_id": mapping.pack_id if mapping else None,
                "gateway_hardware_id": (
                    mapping.gateway_hardware_id if mapping else None
                ),
                "online": slot.slot_id in online_keys,
                "last_metrics": latest["metrics"] if latest else None,
                "last_device_ts": (
                    latest["device_ts"].isoformat() if latest else None
                ),
                "lwt": lwt_by_slot.get(slot.slot_id),
                "pack_alive": lwt_by_slot.get(slot.slot_id) != "offline",
            })
        online_slots = sum(s["online"] for s in slot_out)
        dead_packs = sum(1 for s in slot_out if s["lwt"] == "offline")
        saloon_out.append({
            "id": saloon.pk,
            "name": saloon.name,
            "farm_id": saloon.farm_id,
            "farm_name": saloon.farm.name,
            "flock": _flock_out(flock, cycle_days) if flock else None,
            "slots": slot_out,
        })
        group = farm_groups.setdefault(
            saloon.farm_id,
            {"name": saloon.farm.name, "saloon_count": 0, "online_slots": 0,
             "dead_packs": 0, "saloons": []},
        )
        group["saloon_count"] += 1
        group["online_slots"] += online_slots
        group["dead_packs"] += dead_packs
        group["saloons"].append({"id": saloon.pk})

    return Response({
        "org_id": str(org_id),
        "saloons": saloon_out,
        "farms": list(farm_groups.values()),
    })


# ---------------------------------------------------------------------------
# Per-saloon dashboard cards
# ---------------------------------------------------------------------------

@extend_schema(exclude=True)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_sensors(request, saloon_id: int):
    saloon = Saloon.objects.filter(pk=saloon_id, org_id=_claims_org(request)).first()
    if saloon is None:
        return Response({"error": "saloon not found"}, status=404)
    client = _redis()
    sensors = []
    for mapping in (
        GatewaySensorMapping.objects.filter(slot__saloon_id=saloon_id)
        .select_related("slot")
    ):
        live = bool(client.exists(sensor_status_key(saloon_id, mapping.slot_id)))
        latest = (
            SaloonTelemetry.objects.filter(slot_id=mapping.slot_id)
            .order_by("-device_ts")
            .first()
        )
        metrics = latest.metrics if latest else None
        sensors.append({
            "slot_id": mapping.slot_id,
            "pack_id": mapping.pack_id,
            "health": "online" if live else "offline",
            "online": live,
            "breaching": _breaching_metrics(client, saloon_id, metrics),
            "last_metrics": metrics,
        })
    return Response({
        "saloon_id": saloon_id,
        "sensor_count": len(sensors),
        "health_counts": {
            "online": sum(s["online"] for s in sensors),
            "stale": 0,
            "offline": sum(not s["online"] for s in sensors),
        },
        "sensors": sensors,
    })


def _breaching_metrics(client, saloon_id: int, metrics) -> list:
    """Metrics whose avg is at/above the max in the live threshold hash.
    Falls back to the demo default bands when the hash is absent (scratch
    worlds pre-seed) so the card never lies by showing nothing."""
    if not metrics:
        return []
    thresholds = client.hgetall(thresholds_hash(saloon_id))
    out = []
    for metric, sample in metrics.items():
        if not isinstance(sample, dict) or sample.get("avg") is None:
            continue
        raw_max = thresholds.get(f"{metric}_max")
        maximum = (
            float(raw_max) if raw_max is not None else DEMO_DEFAULT_MAX.get(metric)
        )
        if maximum is not None and float(sample["avg"]) >= maximum:
            out.append(metric)
    return out


@extend_schema(exclude=True)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_stats(request, saloon_id: int):
    saloon = Saloon.objects.filter(pk=saloon_id, org_id=_claims_org(request)).first()
    if saloon is None:
        return Response({"error": "saloon not found"}, status=404)

    window = request.query_params.get("window", "24h")
    if not (window.endswith("h") and window[:-1].isdigit()):
        return Response({"error": "window must be expressed as Nh"}, status=400)
    since = datetime.now(timezone.utc) - timedelta(hours=int(window[:-1]))

    queryset = SaloonTelemetry.objects.filter(
        saloon=saloon, device_ts__gte=since, is_buffered=False
    )
    slot_filter = request.query_params.get("slot_id")
    if slot_filter:
        queryset = queryset.filter(slot_id=slot_filter)

    aggregates = {}
    per_slot = {}
    count = 0
    for row in queryset.iterator():
        count += 1
        slot_stats = per_slot.setdefault(row.slot_id, {})
        for metric, sample in (row.metrics or {}).items():
            if not isinstance(sample, dict) or sample.get("avg") is None:
                continue
            value = float(sample["avg"])
            aggregates.setdefault(metric, []).append(value)
            slot_stats.setdefault(metric, []).append(value)

    def _finish(group):
        result = {}
        for metric, values in group.items():
            result[metric] = {
                "avg": round(sum(values) / len(values), 2),
                "min": min(values),
                "max": max(values),
                "count": len(values),
            }
        return result

    return Response({
        "saloon_id": saloon_id,
        "reading_count": count,
        "stats": _finish(aggregates),
        "per_slot": {slot: _finish(stats) for slot, stats in per_slot.items()},
    })


@extend_schema(exclude=True)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def emqx_status(request):
    return Response({
        "host": "localhost",
        "mqtt_port": 1883,
        "dashboard_port": 18083,
        "mqtt_reachable": False,
        "dashboard_reachable": False,
        "dashboard_url": "http://localhost:18083",
    })


# ---------------------------------------------------------------------------
# One-click publish: drives the REAL pipeline (M3-02/M3-05 shape)
# ---------------------------------------------------------------------------

def _ensure_mapping_for_org(org):
    """Idempotent slot+mapping so publishes have somewhere to land."""
    existing = (
        GatewaySensorMapping.objects.filter(org=org, pack_id="P-A")
        .select_related("slot")
        .first()
    )
    if existing is not None:
        return existing

    tag = org.org_id.hex[:8]
    province = Province.objects.create(name=f"demo-pipe-P-{tag}")
    city = City.objects.create(province=province, name=f"demo-pipe-C-{tag}")
    farm = Farm.objects.create(org=org, name=f"demo-pipe-F-{tag}",
                               province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name="demo-pipe-S")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="demo")
    slot = SaloonSensorSlot.objects.create(
        slot_id=f"slot-demo-{tag}", saloon=saloon, org=org, position=position,
    )
    return GatewaySensorMapping.objects.create(
        gateway_hardware_id=f"hw-demo-{tag}", pack_id="P-A", slot=slot, org=org,
    )


@extend_schema(exclude=True)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def simulate_reading(request):
    """Publish a scripted window through the REAL subscriber pipeline.

    device_ts is bucketed to the minute so pressing the button twice inside
    the same minute reports a duplicate — the dedup guard is what makes the
    dashboard's "publish/dedup" cards honest.
    """
    org_id = _claims_org(request)
    if not org_id:
        return Response({"error": "organization context required"}, status=400)

    org = Organization.objects.get(pk=org_id)
    mapping = _ensure_mapping_for_org(org)
    slot = mapping.slot
    buffered = bool(request.data.get("buffered", False))

    client = _redis()
    bucket = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    device_ts = bucket.isoformat().replace("+00:00", "Z")
    topic = (
        f"{org_id}/farms/{slot.saloon.farm_id}/gateways/{slot.slot_id}/telemetry"
    )
    payload = json.dumps({
        "gateway_hardware_id": mapping.gateway_hardware_id,
        "gateway_slot_id": slot.slot_id,
        "schema_version": "2.0",
        "device_ts": device_ts,
        "is_buffered": buffered,
        "readings": [{
            "pack_id": mapping.pack_id,
            "metrics": {"temp": {"avg": 28.0, "min": 27.0, "max": 29.0},
                        "humidity": {"avg": 60.0},
                        "nh3": {"avg": 12.0, "max": 15.0}},
        }],
    }).encode()

    # Probe BEFORE publishing: the pipeline's own SET NX is what dedups, so
    # here we only predict its verdict for the response card.
    fresh = not client.exists(dedup_key(str(org_id), slot.slot_id, device_ts))
    handle_message(
        client, topic, payload,
        db_fetch=lambda *a: slot.pk,
    )
    drain_stream(STREAM_REALTIME if not buffered else STREAM_HISTORICAL, count=10)

    return Response({
        "outcome": "published" if fresh else "duplicate-dropped",
        "device_ts": device_ts,
        "slot_id": slot.slot_id,
    })