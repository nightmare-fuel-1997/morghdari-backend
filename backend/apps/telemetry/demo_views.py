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

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
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
        .select_related("slot")
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
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if buffered:  # step back an hour so buffered windows are visibly OLD
        now -= timedelta(hours=1)
    device_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    org_id = str(mapping.org_id)
    gateway_slot_topic = mapping.slot.slot_id

    key = dedup_key(org_id, gateway_slot_topic, device_ts)
    was_duplicate = _redis().exists(key) == 1

    payload = json.dumps({
        "gateway_hardware_id": "demo-hw",
        # MUST mirror the topic segment exactly (identity gate cross-checks):
        "gateway_slot_id": gateway_slot_topic,
        "schema_version": "2.0",
        "device_ts": device_ts,
        "is_buffered": buffered,
        "readings": [{"pack_id": "P-A",
                      "metrics": {"temp": {"avg": round(26 + (now.minute % 7) * 0.4, 1)}}}],
    }).encode()

    topic = f"{mapping.org_id}/farms/1/gateways/{gateway_slot_topic}/telemetry"
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
