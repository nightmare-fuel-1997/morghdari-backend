"""M6-02 REST API — the v1.3 KPI + dashboard surface (contract v1.5.0).

- GET/POST /saloons/{saloon_id}/flocks/{flock_id}/weight-samples/
- GET/POST /saloons/{saloon_id}/flocks/{flock_id}/feed-intake/   (per-day upsert)
- GET      /saloons/{saloon_id}/kpis/                            (D22, per request)
- GET/PUT  /me/dashboard-config/
- GET/PUT  /dashboard-config/{role}/

D22 canonical math (Saas.md Decision 22 / v0.6 protocols §3):
- Mortality %  = Σ mortality_records.count ÷ placement_count × 100
- Compliance % = compliant hours ÷ hours-with-data (an hour is compliant when
  every banded metric's hourly average stayed inside the flock-age band from
  lifecycle_threshold_rules); hours with no data are excluded from the
  denominator, not counted non-compliant
- FCR = cumulative feed_kg ÷ ((latest avg_weight_g − day_old_weight_g) × live
  count ÷ 1000); NULL until BOTH a weight sample and feed rows exist — never
  0, never an error. NULL day_old_weight_g is treated as 0 (documented ~1.6 %
  approximation at broiler scale).
- Cycle summary = {days_elapsed, cumulative mortality %, latest FCR,
  alert count, compliance %}.

KPIs are computed per request (no materialization in v1). The `from`/`to`
params window the *daily* series (mortality days, compliance hours); the
cumulative metrics stay cumulative by definition. Same conventions as farms:
org_id comes from the JWT claims and every queryset carries it (RLS is the
Postgres backstop); writes need engineer+ for that saloon; the threshold-band
resolution mirrors alerts.services.thresholds so the alerting engine and the
compliance KPI can never disagree about what "in band" means.

metric_hourly_aggregates shape (Timescale): ONE row per (bucket_hr, slot_id,
metric_key) with avg_val/min/max columns — hour "has data" when any row in
the bucket carries a non-null avg_val.

فارسی: سطح RESTِ KPI نسخهٔ 0.6 — نمونه‌برداری وزن، خوراک روزانه، شاخص‌های D22 و
پیکربندی داشبورد؛ همه‌چیز با فیلتر org از ادعاهای توکن و نقش مهندس برای نوشتن.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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
    FlockWeightSample,
    LifecycleThresholdRule,
)
from apps.farms.models import Saloon
from apps.telemetry.models import FlockCycle, MetricHourlyAggregate

TEHRAN = ZoneInfo("Asia/Tehran")

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
# D22 math — pure helpers (unit-tested directly, no DB)
# ---------------------------------------------------------------------------

def mortality_pct(total_deaths: int, placement_count) -> float | None:
    if not placement_count:
        return None
    return round(total_deaths / placement_count * 100.0, 2)


def fcr_value(total_feed_kg, latest_avg_weight_g, live_count: int, day_old_weight_g):
    """D22 FCR. NULL until both a weight sample and feed data exist — never 0,
    never an error. NULL day_old_weight_g → treated as 0 (documented ~1.6 %
    approximation at broiler scale)."""
    if total_feed_kg is None or latest_avg_weight_g is None:
        return None
    if live_count is None or live_count <= 0:
        return None
    gain_kg = (float(latest_avg_weight_g) - float(day_old_weight_g or 0)) * live_count / 1000.0
    if gain_kg <= 0:
        return None
    return round(float(total_feed_kg) / gain_kg, 3)


def hour_compliant(bucket_rows: list, bands: dict) -> bool:
    """One hour is compliant when every banded metric's hourly average stayed
    inside its flock-age band. Metrics without a band are never judged (D23:
    only metrics with live rules are evaluated). `bucket_rows` is the
    (metric_key, avg_val) pairs of one bucket_hr."""
    for metric_key, avg_val in bucket_rows:
        band = bands.get(metric_key)
        if band is None or avg_val is None:
            continue
        value = float(avg_val)
        lo, hi = band
        if lo is not None and value < float(lo):
            return False
        if hi is not None and value > float(hi):
            return False
    return True


def bands_for_age_days(age_days: int, template_id: int | None) -> dict:
    """{metric_key: (min, max)} for one flock-age day — mirrors
    alerts.services.thresholds._hash_for_flock so compliance and alerting can
    never disagree about what 'in band' means."""
    if template_id is None:
        return {}
    rules = LifecycleThresholdRule.objects.filter(
        template_id=template_id, day_from__lte=age_days, day_to__gte=age_days
    )
    return {rule.metric: (rule.min_value, rule.max_value) for rule in rules}


def compliance_pct(flock: FlockCycle, days: list) -> float | None:
    """Compliant hours ÷ hours-with-data over the given Tehran calendar days.

    NULL when no band rules apply anywhere in the window (nothing to be
    compliant WITH — D23 degradation: unavailable, never 0, never an error)
    or when no hourly data exists at all (hours with no data are excluded,
    not counted non-compliant — the denominator would be empty).
    """
    # Lockstep with the alerting engine: import its resolver rather than
    # duplicating the chain — compliance and alerting can never disagree.
    from apps.alerts.services.thresholds import _resolve_template as _alerting_resolve

    template = _alerting_resolve(flock)
    template_id = template.template_id if template else None
    if template_id is None:
        return None
    start_local = flock.start_date.astimezone(TEHRAN).date()

    # Band windows per day are cheap; bucket the query per day so the
    # flock-age bands stay exact (they change day to day).
    with_data = 0
    compliant = 0
    saw_bands = False
    for day in days:
        age = (day - start_local).days
        if age < 0:
            continue
        bands = bands_for_age_days(age, template_id)
        if not bands:
            continue
        saw_bands = True
        day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=TEHRAN)
        day_end = day_start + timedelta(days=1)
        rows = (
            MetricHourlyAggregate.objects.filter(
                org_id=flock.org_id,
                saloon_id=flock.saloon_id,
                bucket_hr__gte=day_start,
                bucket_hr__lt=day_end,
            )
            .order_by("bucket_hr")
            .values_list("bucket_hr", "metric_id", "avg_val")
        )
        # Group the (bucket, metric, avg) rows into per-bucket pairs.
        current_bucket = None
        bucket_pairs: list = []
        for bucket_hr, metric_key, avg_val in rows:
            if bucket_hr != current_bucket:
                if current_bucket is not None and bucket_pairs:
                    with_data += 1
                    if hour_compliant(bucket_pairs, bands):
                        compliant += 1
                current_bucket = bucket_hr
                bucket_pairs = []
            bucket_pairs.append((metric_key, avg_val))
        if current_bucket is not None and bucket_pairs:
            with_data += 1
            if hour_compliant(bucket_pairs, bands):
                compliant += 1
    if not saw_bands or with_data == 0:
        return None
    return round(compliant / with_data * 100.0, 2)


def _parse_date_param(request, name: str):
    raw = request.query_params.get(name)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


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

    cum_mortality = mortality_pct(total_deaths, flock.placement_count)

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
    compliance = compliance_pct(flock, window_days)

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
    fcr = fcr_value(
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
