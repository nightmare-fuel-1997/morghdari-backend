"""M6-02 REST API — the v1.3 KPI + dashboard surface (contract v1.5.0).

- GET/POST /saloons/{saloon_id}/flocks/{flock_id}/weight-samples/
- GET/POST /saloons/{saloon_id}/flocks/{flock_id}/feed-intake/   (per-day upsert)
- GET      /saloons/{saloon_id}/kpis/                            (D22, per request)
- GET/PUT  /me/dashboard-config/
- GET/PUT  /dashboard-config/{role}/

M6-03 adds the v1.4 water-intake sibling (same per-day upsert + audited
corrections); biosecurity-events and capabilities live in production_views.

D22 math lives in services/kpi.py (M6-04) — this module assembles the HTTP
surface: resolve flock/window, gather inputs, delegate to kpi_service, shape
responses. The threshold-band helpers live in that ONE module so alerting
(alerts.services.thresholds) and the compliance KPI can never disagree about
what "in band" means. water:feed ratio (M6-04) ships as service math; its
/kpis/ exposure awaits the M6-10 contract ratification (yaml is frozen until
then — service is the canonical home meanwhile).

KPIs are computed per request (no materialization in v1). The `from`/`to`
params window the *daily* series (mortality days, compliance hours); the
cumulative metrics stay cumulative by definition. Same conventions as farms:
org_id comes from the JWT claims and every queryset carries it (RLS is the
Postgres backstop); writes need engineer+ for that saloon.

metric_hourly_aggregates shape (Timescale): ONE row per (bucket_hr, slot_id,
metric_key) with avg_val/min/max columns — hour "has data" when any row in
the bucket carries a non-null avg_val.

فارسی: سطح RESTِ KPI نسخهٔ 0.6 — نمونه‌برداری وزن، خوراک روزانه، شاخص‌های D22 و
پیکربندی داشبورد؛ همه‌چیز با فیلتر org از ادعاهای توکن و نقش مهندس برای نوشتن.
"""

from datetime import date, timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.alerts.models import (
    AlertInstance,
    DashboardConfig,
    FlockFeedIntake,
    FlockWaterIntake,
    FlockWeightSample,
)
from apps.alerts.services import kpi as kpi_service
from apps.alerts.services.kpi import TEHRAN
from apps.farms.models import Saloon
from apps.telemetry.models import FlockCycle

# Engineer-and-above for the production-entry surfaces; any org member reads.
WRITE_ROLES = {"engineer", "manager", "owner"}
# DashboardConfig role rows are an owner-level surface (D22 downstream rule);
# "admin" covers the platform-staff role when present in the claims.
ROLE_ADMIN_ROLES = {"owner", "admin"}
DASHBOARD_ROLE_ENUM = ["owner", "manager", "engineer", "doctor", "worker"]
ROLE_PRECEDENCE = ["owner", "manager", "engineer", "doctor", "worker"]

# Built-in system default: every widget on (DashboardConfig docstring list).
SYSTEM_WIDGETS = {
    "live_readings": True,
    "mortality_pct": True,
    "compliance_pct": True,
    "fcr": True,
    "cycle_summary": True,
    "alerts": True,
    "feed_intake": True,
    "weight_trend": True,
}

KPI_METRICS = ["mortality_pct", "compliance_pct", "fcr", "cycle_summary"]
FEED_TYPES = {"starter", "grower", "finisher"}


def _org_id(request):
    return _claims(request).get("org_id")


def _roles(request):
    return set(_claims(request).get("roles", []))


def _role_context(request) -> str | None:
    """The caller's current role context = highest-precedence claimed role."""
    roles = _roles(request)
    for role in ROLE_PRECEDENCE:
        if role in roles:
            return role
    return next(iter(roles), None)


def _saloon_in_org(saloon_id: int, org_id) -> Saloon | None:
    return Saloon.objects.filter(org_id=org_id, pk=saloon_id).first()


def _flock_for_saloon(flock_id: int, saloon: Saloon, org_id) -> FlockCycle | None:
    """The requested flock must exist, belong to the org AND this saloon."""
    return FlockCycle.objects.filter(org_id=org_id, pk=flock_id, saloon_id=saloon.pk).first()


def _resolve_flock(request, saloon: Saloon):
    """flock_id param wins; else the active flock; else the most recent."""
    raw = request.query_params.get("flock_id")
    if raw:
        return FlockCycle.objects.filter(saloon_id=saloon.pk, pk=raw).first()
    return (
        FlockCycle.objects.filter(saloon_id=saloon.pk, end_date__isnull=True)
        .order_by("-start_date")
        .first()
        or FlockCycle.objects.filter(saloon_id=saloon.pk).order_by("-start_date").first()
    )


# ---------------------------------------------------------------------------
# D22 math lives in services/kpi.py (M6-04) — pure functions + compliance,
# unit-tested without a DB; the view below only assembles inputs.
# ---------------------------------------------------------------------------


def _parse_date_param(request, name: str):
    raw = request.query_params.get(name)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _log_intake_row_out(row) -> dict:
    """Shared GET shape for the feed/water daily-intake listings (v1.3/v1.4)."""
    return {
        "entry_date": row.entry_date.isoformat(),
        "source": row.source,
    }


def _parse_iso_dt(raw):
    parsed = parse_datetime(str(raw))
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TEHRAN)
    return parsed


def _sample_out(row: FlockWeightSample) -> dict:
    return {
        "sampled_at": row.sampled_at.isoformat(),
        "sample_size": row.sample_size,
        "avg_weight_g": float(row.avg_weight_g),
        "uniformity_pct": (
            float(row.uniformity_pct) if row.uniformity_pct is not None else None
        ),
        "source": row.source,
    }


# ---------------------------------------------------------------------------
# GET/POST /saloons/{saloon_id}/flocks/{flock_id}/weight-samples/
# ---------------------------------------------------------------------------
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def weight_samples(request, saloon_id: int, flock_id: int):
    org_id = _org_id(request)
    saloon = _saloon_in_org(saloon_id, org_id) if org_id else None
    if saloon is None:
        return Response({"error": "saloon not found"}, status=404)
    flock = _flock_for_saloon(flock_id, saloon, org_id)
    if flock is None:
        return Response({"error": "flock not found"}, status=404)

    if request.method == "GET":
        rows = FlockWeightSample.objects.filter(flock=flock, org_id=org_id).order_by(
            "-sampled_at"
        )
        return Response([_sample_out(row) for row in rows])

    # POST — engineer+ (D22 downstream rule: manual entry by the Farm Engineer)
    if not (_roles(request) & WRITE_ROLES):
        return Response({"error": "engineer role required"}, status=403)

    try:
        sample_size = int(request.data.get("sample_size"))
    except (TypeError, ValueError):
        return Response(
            {"error": "sample_size required and must be an integer"}, status=400
        )
    if sample_size < 1:
        return Response({"error": "sample_size must be ≥ 1"}, status=400)

    def _num(key):
        raw = request.data.get(key)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ValueError(key)  # noqa: TRY003 — carried to the 400 below

    try:
        avg_weight_g = _num("avg_weight_g")
        min_weight_g = _num("min_weight_g")
        max_weight_g = _num("max_weight_g")
        uniformity_pct = _num("uniformity_pct")
    except ValueError as exc:
        return Response({"error": f"{exc.args[0]} must be numeric"}, status=400)
    if avg_weight_g is None:
        return Response({"error": "avg_weight_g required"}, status=400)

    sampled_at = _parse_iso_dt(request.data.get("sampled_at") or "") or timezone.now()

    row = FlockWeightSample.objects.create(
        org_id=org_id,
        flock=flock,
        saloon=saloon,
        sampled_at=sampled_at,
        sample_size=sample_size,
        avg_weight_g=avg_weight_g,
        min_weight_g=min_weight_g,
        max_weight_g=max_weight_g,
        uniformity_pct=uniformity_pct,
        recorded_by=request.user,
        notes=str(request.data.get("notes") or "").strip() or None,
    )
    return Response(_sample_out(row), status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# GET/POST /saloons/{saloon_id}/flocks/{flock_id}/feed-intake/
# ---------------------------------------------------------------------------
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def feed_intake(request, saloon_id: int, flock_id: int):
    org_id = _org_id(request)
    saloon = _saloon_in_org(saloon_id, org_id) if org_id else None
    if saloon is None:
        return Response({"error": "saloon not found"}, status=404)
    flock = _flock_for_saloon(flock_id, saloon, org_id)
    if flock is None:
        return Response({"error": "flock not found"}, status=404)

    if request.method == "GET":
        rows = FlockFeedIntake.objects.filter(flock=flock, org_id=org_id).order_by(
            "entry_date"
        )
        return Response([
            {
                "entry_date": row.entry_date.isoformat(),
                "feed_kg": float(row.feed_kg),
                "feed_type": row.feed_type,
                "source": row.source,
            }
            for row in rows
        ])

    if not (_roles(request) & WRITE_ROLES):
        return Response({"error": "engineer role required"}, status=403)

    try:
        entry_date = date.fromisoformat(str(request.data.get("entry_date", "")))
    except ValueError:
        return Response({"error": "entry_date required (YYYY-MM-DD)"}, status=400)

    try:
        feed_kg = float(request.data.get("feed_kg"))
    except (TypeError, ValueError):
        return Response(
            {"error": "feed_kg required and must be numeric"}, status=400
        )

    feed_type = request.data.get("feed_type")
    if feed_type is not None and feed_type not in FEED_TYPES:
        return Response(
            {"error": "feed_type must be one of " + ", ".join(sorted(FEED_TYPES))},
            status=400,
        )

    # ONE row per flock per day — re-posting the same date UPDATES it and the
    # correction is audited (D22: corrections are audited via activity logs).
    with transaction.atomic():
        existing = (
            FlockFeedIntake.objects.select_for_update()
            .filter(flock=flock, entry_date=entry_date)
            .first()
        )
        corrected = existing is not None
        row = FlockFeedIntake.objects.update_or_create(
            flock=flock,
            entry_date=entry_date,
            defaults={
                "org_id": org_id,
                "saloon": saloon,
                "feed_kg": feed_kg,
                "feed_type": feed_type,
                "source": "manual",
                "recorded_by": request.user,
                "notes": str(request.data.get("notes") or "").strip() or None,
            },
        )[0]
        if corrected:
            from apps.auditing.models import AccountActivityLog

            AccountActivityLog.objects.create(
                actor=request.user,
                action="feed_intake_correction",
                org_id=org_id,
                saloon=saloon,
                details={
                    "flock_id": flock.pk,
                    "entry_date": entry_date.isoformat(),
                    "old_value": float(existing.feed_kg),
                    "new_value": float(feed_kg),
                },
            )
    return Response(
        {
            "entry_date": row.entry_date.isoformat(),
            "feed_kg": float(row.feed_kg),
            "feed_type": row.feed_type,
            "source": row.source,
            "corrected": corrected,
        },
        status=status.HTTP_200_OK,
    )


# ---------------------------------------------------------------------------
# GET/POST /saloons/{saloon_id}/flocks/{flock_id}/water-intake/   (M6-03, v1.4)
# ---------------------------------------------------------------------------
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def water_intake(request, saloon_id: int, flock_id: int):
    """The feed-intake twin: ONE row per flock per day, corrections are
    UPDATEs audited exactly like feed corrections (yaml: "corrections are
    UPDATEs, audited"; the device lane later writes source=device rows)."""
    org_id = _org_id(request)
    saloon = _saloon_in_org(saloon_id, org_id) if org_id else None
    if saloon is None:
        return Response({"error": "saloon not found"}, status=404)
    flock = _flock_for_saloon(flock_id, saloon, org_id)
    if flock is None:
        return Response({"error": "flock not found"}, status=404)

    if request.method == "GET":
        rows = FlockWaterIntake.objects.filter(flock=flock, org_id=org_id).order_by(
            "entry_date"
        )
        date_from = _parse_date_param(request, "from")
        if date_from:
            rows = rows.filter(entry_date__gte=date_from)
        date_to = _parse_date_param(request, "to")
        if date_to:
            rows = rows.filter(entry_date__lte=date_to)
        out = []
        for row in rows:
            item = _log_intake_row_out(row)
            item["water_l"] = float(row.water_l)
            out.append(item)
        return Response(out)

    if not (_roles(request) & WRITE_ROLES):
        return Response({"error": "engineer role required"}, status=403)

    try:
        entry_date = date.fromisoformat(str(request.data.get("entry_date", "")))
    except ValueError:
        return Response({"error": "entry_date required (YYYY-MM-DD)"}, status=400)

    try:
        water_l = float(request.data.get("water_l"))
    except (TypeError, ValueError):
        return Response(
            {"error": "water_l required and must be numeric"}, status=400
        )

    # ONE row per flock per day — re-posting the same date UPDATES it and the
    # correction is audited (same D22 rule as feed_intake above).
    with transaction.atomic():
        existing = (
            FlockWaterIntake.objects.select_for_update()
            .filter(flock=flock, entry_date=entry_date)
            .first()
        )
        corrected = existing is not None
        row = FlockWaterIntake.objects.update_or_create(
            flock=flock,
            entry_date=entry_date,
            defaults={
                "org_id": org_id,
                "saloon": saloon,
                "water_l": water_l,
                "source": "manual",
                "recorded_by": request.user,
                "notes": str(request.data.get("notes") or "").strip() or None,
            },
        )[0]
        if corrected:
            from apps.auditing.models import AccountActivityLog

            AccountActivityLog.objects.create(
                actor=request.user,
                action="water_intake_correction",
                org_id=org_id,
                saloon=saloon,
                details={
                    "flock_id": flock.pk,
                    "entry_date": entry_date.isoformat(),
                    "old_value": float(existing.water_l),
                    "new_value": float(water_l),
                },
            )
    return Response(
        {
            "entry_date": row.entry_date.isoformat(),
            "water_l": float(row.water_l),
            "source": row.source,
            "corrected": corrected,
        },
        status=status.HTTP_200_OK,
    )


# ---------------------------------------------------------------------------
# GET /saloons/{saloon_id}/kpis/
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_kpis(request, saloon_id: int):
    org_id = _org_id(request)
    saloon = _saloon_in_org(saloon_id, org_id) if org_id else None
    if saloon is None:
        return Response({"error": "saloon not found"}, status=404)
    flock = _resolve_flock(request, saloon)
    if flock is None:
        return Response({"error": "no flock found"}, status=404)

    requested = [
        m.strip()
        for m in str(
            request.query_params.get("metrics", ",".join(KPI_METRICS))
        ).split(",")
        if m.strip()
    ]
    unknown = [m for m in requested if m not in KPI_METRICS]
    if unknown:
        return Response(
            {"error": "unknown metrics: " + ", ".join(unknown)}, status=400
        )

    date_from = _parse_date_param(request, "from")
    date_to = _parse_date_param(request, "to")
    if date_from and date_to and date_from > date_to:
        return Response({"error": "from must be ≤ to"}, status=400)

    from apps.alerts.models import MortalityRecord

    today_local = timezone.now().astimezone(TEHRAN).date()
    start_local = flock.start_date.astimezone(TEHRAN).date()
    end_local = flock.end_date.astimezone(TEHRAN).date() if flock.end_date else None

    # ---- cumulative mortality (per Tehran day for the daily series) ----------
    day_totals: dict = {}
    total_deaths = 0
    for record in MortalityRecord.objects.filter(flock=flock, org_id=org_id).only(
        "count", "occurred_at"
    ):
        day = record.occurred_at.astimezone(TEHRAN).date()
        day_totals[day] = day_totals.get(day, 0) + record.count
        total_deaths += record.count

    cum_mortality, _ = kpi_service.mortality_pct(total_deaths, flock.placement_count)

    # ---- daily mortality over the window ------------------------------------
    first_day = date_from or start_local
    last_day = min(date_to or today_local, end_local or today_local, today_local)
    daily = []
    cumulative = 0
    for day in sorted(day_totals):
        if day < first_day or day > last_day:
            continue
        cumulative += day_totals[day]
        live = flock.placement_count - cumulative  # birds alive through that day
        daily.append({
            "date": day.isoformat(),
            "pct": (
                round(day_totals[day] / live * 100.0, 2) if live > 0 else None
            ),
        })

    # ---- compliance (same window as the daily series) ------------------------
    window_days = []
    cursor = first_day
    while cursor <= last_day and len(window_days) < 400:  # a cycle is ~45 days
        window_days.append(cursor)
        cursor += timedelta(days=1)
    compliance, _ = kpi_service.compliance_pct(flock, window_days)

    # ---- FCR ------------------------------------------------------------------
    latest_sample = (
        FlockWeightSample.objects.filter(flock=flock, org_id=org_id)
        .order_by("-sampled_at")
        .first()
    )
    total_feed = (
        FlockFeedIntake.objects.filter(flock=flock, org_id=org_id)
        .aggregate(total=Sum("feed_kg"))["total"]
    )
    live_count = flock.placement_count - total_deaths
    fcr, _ = kpi_service.fcr_value(
        float(total_feed) if total_feed is not None else None,
        latest_sample.avg_weight_g if latest_sample else None,
        live_count,
        flock.day_old_weight_g,
    )

    # ---- cycle summary --------------------------------------------------------
    days_elapsed = max(((end_local or today_local) - start_local).days, 0)

    out = {"flock_id": flock.flock_id}
    if "mortality_pct" in requested:
        out["mortality_pct"] = cum_mortality
        out["daily_mortality_pct"] = daily
    if "compliance_pct" in requested:
        out["compliance_pct"] = compliance
    if "fcr" in requested:
        out["fcr"] = fcr
    if "cycle_summary" in requested:
        out["cycle_summary"] = {
            "days_elapsed": days_elapsed,
            "cumulative_mortality_pct": cum_mortality,
            "latest_fcr": fcr,
            "alert_count": AlertInstance.objects.filter(
                org_id=org_id, saloon_id=saloon.pk, source="REALTIME"
            ).count(),
            "compliance_pct": compliance,
        }
    return Response(out)


# ---------------------------------------------------------------------------
# Dashboard config — resolution order personal → role default → system default
# ---------------------------------------------------------------------------

def _resolved_config(org_id, user, role: str) -> dict:
    personal = DashboardConfig.objects.filter(org_id=org_id, user=user, role=role).first()
    role_row = DashboardConfig.objects.filter(
        org_id=org_id, role=role, user__isnull=True
    ).first()
    widgets: dict = {}
    resolved_from: dict = {}
    for key, system_value in SYSTEM_WIDGETS.items():
        if personal and key in personal.widgets:
            widgets[key] = bool(personal.widgets[key])
            resolved_from[key] = "personal"
        elif role_row and key in role_row.widgets:
            widgets[key] = bool(role_row.widgets[key])
            resolved_from[key] = "role_default"
        else:
            widgets[key] = system_value
            resolved_from[key] = "system_default"
    return {"role": role, "widgets": widgets, "resolved_from": resolved_from}


def _validate_widgets(raw) -> dict | None:
    """None → invalid payload; else the {key: bool} dict (system keys only)."""
    if not isinstance(raw, dict):
        return None
    widgets = {}
    for key, value in raw.items():
        if key not in SYSTEM_WIDGETS or not isinstance(value, bool):
            return None
        widgets[key] = value
    return widgets


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def me_dashboard_config(request):
    org_id = _org_id(request)
    if not org_id:
        return Response({"error": "organization context required"}, status=400)
    role = _role_context(request)
    if role is None:
        return Response({"error": "no role in token claims"}, status=403)

    if request.method == "GET":
        return Response(_resolved_config(org_id, request.user, role))

    # PUT — write ONLY the caller's own personal row; partial updates allowed
    # (keys not sent keep their current resolution — merged onto the row).
    widgets = _validate_widgets(request.data.get("widgets"))
    if widgets is None:
        return Response(
            {"error": "widgets must map known widget keys to booleans"}, status=400
        )
    with transaction.atomic():
        row = DashboardConfig.objects.filter(
            org_id=org_id, user=request.user, role=role
        ).first()
        merged = dict(row.widgets) if row else {}
        merged.update(widgets)
        DashboardConfig.objects.update_or_create(
            org_id=org_id,
            user=request.user,
            role=role,
            defaults={"widgets": merged, "updated_by": request.user},
        )
    return Response(_resolved_config(org_id, request.user, role))


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def role_dashboard_config(request, role: str):
    org_id = _org_id(request)
    if not org_id:
        return Response({"error": "organization context required"}, status=400)
    if role not in DASHBOARD_ROLE_ENUM:
        return Response({"error": "unknown role"}, status=400)

    if request.method == "GET":
        role_row = DashboardConfig.objects.filter(
            org_id=org_id, role=role, user__isnull=True
        ).first()
        if role_row is None:
            return Response({
                "role": role,
                "widgets": dict(SYSTEM_WIDGETS),
                "is_system_default": True,
            })
        return Response({
            "role": role,
            "widgets": dict(role_row.widgets),
            "is_system_default": False,
        })

    # PUT — owner/admin only (D22: role-default rows are an owner surface).
    if not (_roles(request) & ROLE_ADMIN_ROLES):
        return Response({"error": "owner role required"}, status=403)

    widgets = _validate_widgets(request.data.get("widgets"))
    if widgets is None:
        return Response(
            {"error": "widgets must map known widget keys to booleans"}, status=400
        )
    DashboardConfig.objects.update_or_create(
        org_id=org_id,
        role=role,
        user=None,
        defaults={"widgets": widgets, "updated_by": request.user},
    )
    return Response({"role": role, "widgets": widgets, "is_system_default": False})
