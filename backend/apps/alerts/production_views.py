"""M6-03 REST API — the v1.4 production/biosecurity surface (contract v1.5.0).

- GET/POST /farms/{farm_id}/biosecurity-events/   (avian-flu evidence log)
- GET      /saloons/{saloon_id}/capabilities/     (D23 capability profile)

Biosecurity: farm-level (not saloon); any org member records, owner/admin
review later; every field the vet/insurance export needs (GS-EVIDENCE). The
event_type enum is enforced by the ck_biosecurity_events_event_type check —
validation here just turns the DB rejection into a friendly 400.

Capabilities: D23 computed truth — live metrics from ACTIVE sensor slots'
allowed_metric_keys ∪ keys seen in recent telemetry; log availability from row
existence for the caller's currently-resolved flock. The RULES live in
services.capability_profile.FEATURE_MATRIX (the canonical declared form; the
endpoint stays thin and M6-05 extends the matrix without touching this view).

Conventions are the M6-01/02 ones: org from the VALIDATED org_id claim on
every queryset (RLS is the Postgres backstop); cross-org ids → 404;
`{"error": ...}` envelopes.

فارسی: ثبت رویدادهای زیست‌امنیتی برای همهٔ اعضای سازمان و نمای توانمندی سالن —
قانون در FEATURE_MATRIX، واقعیت از سنسورها و لاگ‌ها؛ همیشه «در دسترس نیست»،
هرگز «عادی» و هرگز صفر.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.alerts.models import BiosecurityEvent, FlockFeedIntake, FlockWaterIntake, FlockWeightSample
from apps.alerts.services.capability_profile import compute_profile
from apps.alerts.services.kpi import TEHRAN
from apps.devices.models import SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.telemetry.models import SaloonTelemetry

# "recent telemetry" horizon for derived/seen keys (TUTORIAL §3.4: allowed ∪ seen).
RECENT_TELEMETRY_WINDOW = timedelta(hours=24)

BIOSECURITY_EVENT_TYPES = {
    "visitor_entry",
    "vehicle_entry",
    "disinfection",
    "rodent_control",
    "outbreak_prep",
    "other",
}


def _org_id(request):
    return _claims(request).get("org_id")


# ---------------------------------------------------------------------------
# GET/POST /farms/{farm_id}/biosecurity-events/   (M6-03, v1.4)
# ---------------------------------------------------------------------------
def _bio_event_out(row: BiosecurityEvent) -> dict:
    return {
        "id": row.pk,
        "event_type": row.event_type,
        "occurred_at": row.occurred_at.isoformat(),
        "person_count": row.person_count,
        "recorded_by": row.recorded_by_id,
        "details": row.details,
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def biosecurity_events(request, farm_id: int):
    org_id = _org_id(request)
    # Cross-org reads/writes die here with 404 — RLS is the Postgres backstop.
    farm = Farm.objects.filter(org_id=org_id, pk=farm_id).first() if org_id else None
    if farm is None:
        return Response({"error": "farm not found"}, status=404)

    if request.method == "GET":
        rows = BiosecurityEvent.objects.filter(farm=farm, org_id=org_id).order_by("-occurred_at")
        event_type = request.query_params.get("event_type")
        if event_type:
            rows = rows.filter(event_type=event_type)
        date_from = request.query_params.get("from")
        if date_from:
            parsed = _parse_dt(date_from)
            if parsed is None:
                return Response({"error": "from must be an ISO date-time"}, status=400)
            rows = rows.filter(occurred_at__gte=parsed)
        date_to = request.query_params.get("to")
        if date_to:
            parsed = _parse_dt(date_to)
            if parsed is None:
                return Response({"error": "to must be an ISO date-time"}, status=400)
            rows = rows.filter(occurred_at__lte=parsed)
        return Response([_bio_event_out(row) for row in rows])

    # POST — any org member records (yaml: "any staff role may record").
    event_type = request.data.get("event_type")
    if event_type not in BIOSECURITY_EVENT_TYPES:
        return Response(
            {"error": "event_type must be one of " + ", ".join(sorted(BIOSECURITY_EVENT_TYPES))},
            status=400,
        )
    occurred_at = _parse_dt(request.data.get("occurred_at") or "") or timezone.now()
    person_count = request.data.get("person_count")
    if person_count is not None:
        try:
            person_count = int(person_count)
        except (TypeError, ValueError):
            return Response({"error": "person_count must be an integer"}, status=400)
    details = request.data.get("details")
    if details is not None and not isinstance(details, dict):
        return Response({"error": "details must be an object"}, status=400)

    row = BiosecurityEvent.objects.create(
        org_id=org_id,
        farm=farm,
        event_type=event_type,
        occurred_at=occurred_at,
        person_count=person_count,
        details=details,
        recorded_by=request.user,
    )
    return Response(_bio_event_out(row), status=201)


def _parse_dt(raw):
    from django.utils.dateparse import parse_datetime

    from apps.alerts.services.kpi import TEHRAN

    parsed = parse_datetime(str(raw))
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TEHRAN)
    return parsed


# ---------------------------------------------------------------------------
# GET /saloons/{saloon_id}/capabilities/   (M6-03, v1.4 — D23 computed truth)
# ---------------------------------------------------------------------------
def _live_metric_keys(saloon: Saloon) -> set:
    """allowed_metric_keys of ACTIVE slots ∪ keys seen in recent telemetry.
    The telemetry half is Postgres-only: saloon_telemetry is an UNMANAGED
    hypertable that SQLite test runs legitimately do not have (the slots half
    — the production truth — always applies)."""
    from django.db import connection

    live: set = set()
    for slot in SaloonSensorSlot.objects.filter(saloon=saloon, is_active=True).only(
        "allowed_metric_keys"
    ):
        live.update(slot.allowed_metric_keys or [])
    if connection.vendor != "postgresql":
        return live
    since = timezone.now() - RECENT_TELEMETRY_WINDOW
    for row in (
        SaloonTelemetry.objects.filter(saloon=saloon, server_received_ts__gte=since)
        .only("metrics")
        .order_by("-server_received_ts")
        .distinct()[:200]
    ):
        live.update((row.metrics or {}).keys())
    return live


def _available_logs(saloon: Saloon, org_id) -> dict:
    """Row-existence truth for the manual-log features — resolved flock first,
    else any row for the saloon (a farm without a registered flock can still
    see what logging it HAS, and what it is missing).

    M6-05: also computes the COMPUTED availability facts for the D22 KPI
    features (compliance needs threshold bands for the flock-age window and
    hours-with-data — the same facts kpi_service.compliance_pct reports).
    hourly_data is Postgres-only (metric_hourly_aggregates is an unmanaged
    hypertable; SQLite test runs legitimately lack it)."""
    from django.db import connection

    from apps.alerts.services.kpi import bands_for_age_days
    from apps.alerts.services.thresholds import _resolve_template
    from apps.telemetry.models import FlockCycle

    flock = (
        FlockCycle.objects.filter(saloon=saloon, org_id=org_id, end_date__isnull=True)
        .order_by("-start_date")
        .first()
        or FlockCycle.objects.filter(saloon=saloon, org_id=org_id)
        .order_by("-start_date")
        .first()
    )
    scope = {"flock": flock} if flock else {"saloon": saloon}
    available = {
        "weight_samples": FlockWeightSample.objects.filter(org_id=org_id, **scope).exists(),
        "feed_intake": FlockFeedIntake.objects.filter(org_id=org_id, **scope).exists(),
        "water_intake": FlockWaterIntake.objects.filter(org_id=org_id, **scope).exists(),
        "placement": flock is not None and flock.placement_count > 0,
        "mortality_records": False,
        "threshold_bands": False,
        "hourly_data": False,
    }
    if flock is None:
        return available

    from apps.alerts.models import MortalityRecord

    available["mortality_records"] = MortalityRecord.objects.filter(
        org_id=org_id, flock=flock
    ).exists()

    template = _resolve_template(flock)
    if template is not None:
        start_local = flock.start_date.astimezone(TEHRAN).date()
        end_local = (flock.end_date or timezone.now()).astimezone(TEHRAN).date()
        day = start_local
        while day <= end_local:
            if bands_for_age_days((day - start_local).days, template.template_id):
                available["threshold_bands"] = True
                break
            day += timedelta(days=1)

    if connection.vendor == "postgresql":
        from apps.telemetry.models import MetricHourlyAggregate

        available["hourly_data"] = MetricHourlyAggregate.objects.filter(
            org_id=org_id, saloon_id=saloon.pk
        ).exists()
    return available


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_capabilities(request, saloon_id: int):
    org_id = _org_id(request)
    saloon = Saloon.objects.filter(org_id=org_id, pk=saloon_id).first() if org_id else None
    if saloon is None:
        return Response({"error": "saloon not found"}, status=404)
    return Response(
        compute_profile(
            saloon.pk,
            _live_metric_keys(saloon),
            _available_logs(saloon, org_id),
        )
    )
