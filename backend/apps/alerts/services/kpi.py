"""KPI computation service — D22 canonical math (M6-04).

D22 (Saas.md): KPIs are computed per request in v1 from the production tables
+ hourly aggregates; every number is NULL-with-reason until its inputs exist —
never 0, never an error. Each function returns ``(value, reason)``: the tuple
IS decisions D22+D23 expressed as types (TUTORIAL §3.3).

Canonical definitions (D22, verbatim intent):
- Mortality %  = cumulative deaths ÷ placement_count × 100 (daily variant:
  day's deaths ÷ live count = placement_count − cumulative deaths).
- FCR = cumulative feed_kg ÷ ((latest avg_weight_g − day_old_weight_g) × live
  count ÷ 1000); NULL until BOTH a weight sample and feed data exist; NULL
  day_old_weight_g → treated as 0 (documented ~1.6 % approximation).
- Water:feed ratio = cumulative water_l ÷ cumulative feed_kg — industry band
  ~1.8–2:1 (business-numbers-v1.md); out-of-band is an early-disease signal,
  NOT an alert. NULL until BOTH water and feed logs exist. Endpoint exposure
  of the metric is a contract change — it enters /kpis/ only via the M6-10
  ratification commit (the service is the canonical home meanwhile).
- Cycle summary = {days_elapsed, cumulative mortality %, latest FCR,
  alert count, compliance %} for the flock.

The threshold-band helpers (bands_for_age_days / hour_compliant /
compliance_pct) live here too so alerting (alerts.services.thresholds) and
the compliance KPI can never disagree about what "in band" means — the view
imports them from this ONE module. Named §2.7 suites: test_fcr_math,
test_kpi_mortality_pct, test_compliance_pct (endpoint-level, in
test_m6_02_kpi.py), test_water_feed_ratio (D23 §2.8).

فارسی: ریاضیِ KPI در ONE سرویس — تا موتور هشدار و شاخصِ انطباق هرگز دربارهٔ
«درون باند» اختلاف نظر پیدا نکنند؛ ورودیِ غایب یعنی NULL با دلیل، نه صفر.
"""

from zoneinfo import ZoneInfo

from apps.alerts.models import LifecycleThresholdRule
from apps.telemetry.models import MetricHourlyAggregate

# ONE Tehran clock constant for the whole KPI surface (views import it here).
TEHRAN = ZoneInfo("Asia/Tehran")

# Reasons travel with NULL (D23 degradation rule).
REASON_NO_PLACEMENT = "placement_count not set"
REASON_FCR_INPUTS = "needs a weight sample and feed data"
REASON_NO_LIVE = "live count unknown or zero"
REASON_NO_WATER = "needs water logs"
REASON_NO_FEED = "needs feed logs"
REASON_NO_BANDS = "no threshold bands for the flock-age window"
REASON_NO_DATA = "no hourly data in the window"


# ---------------------------------------------------------------------------
# Pure math — unit-testable without a database
# ---------------------------------------------------------------------------

def mortality_pct(total_deaths: int, placement_count) -> tuple:
    """D22 mortality: cumulative deaths ÷ placement_count × 100. Zero deaths
    is a true 0.0 (no NULL clause in D22 beyond the placement guard)."""
    if not placement_count:
        return None, REASON_NO_PLACEMENT
    return round(total_deaths / placement_count * 100.0, 2), None


def fcr_value(total_feed_kg, latest_avg_weight_g, live_count, day_old_weight_g) -> tuple:
    """D22 FCR. NULL until both a weight sample and feed data exist — never 0,
    never an error. NULL day_old_weight_g → treated as 0 (documented ~1.6 %
    approximation at broiler scale)."""
    if total_feed_kg is None or latest_avg_weight_g is None:
        return None, REASON_FCR_INPUTS
    if live_count is None or live_count <= 0:
        return None, REASON_NO_LIVE
    gain_kg = (float(latest_avg_weight_g) - float(day_old_weight_g or 0)) * live_count / 1000.0
    if gain_kg <= 0:
        return None, REASON_NO_LIVE
    return round(float(total_feed_kg) / gain_kg, 3), None


def water_feed_ratio(total_water_l, total_feed_kg) -> tuple:
    """D22/D23 water:feed ratio (industry band ~1.8–2:1). NULL until BOTH
    water and feed logs exist for the period — never 0, never an error."""
    if total_water_l is None:
        return None, REASON_NO_WATER
    if total_feed_kg is None:
        return None, REASON_NO_FEED
    if float(total_feed_kg) <= 0:
        return None, REASON_NO_FEED
    return round(float(total_water_l) / float(total_feed_kg), 2), None


def build_cycle_summary(
    days_elapsed: int, cumulative_mortality, latest_fcr, alert_count: int, compliance
) -> dict:
    """D22 cycle summary — {days elapsed, cumulative mortality %, latest FCR,
    alert count, compliance %}."""
    return {
        "days_elapsed": days_elapsed,
        "cumulative_mortality_pct": cumulative_mortality,
        "latest_fcr": latest_fcr,
        "alert_count": alert_count,
        "compliance_pct": compliance,
    }


# ---------------------------------------------------------------------------
# Compliance — same source of truth as the alerting threshold bands
# ---------------------------------------------------------------------------

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


def compliance_pct(flock, days: list) -> tuple:
    """Compliant hours ÷ hours-with-data over the given Tehran calendar days.

    NULL when no band rules apply anywhere in the window (nothing to be
    compliant WITH — D23 degradation: unavailable, never 0, never an error)
    or when no hourly data exists at all (hours with no data are excluded,
    not counted non-compliant — the denominator would be empty).
    """
    from datetime import datetime, timedelta

    from apps.alerts.services.thresholds import _resolve_template as _alerting_resolve

    template = _alerting_resolve(flock)
    template_id = template.template_id if template else None
    if template_id is None:
        return None, REASON_NO_BANDS
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
    if not saw_bands:
        return None, REASON_NO_BANDS
    if with_data == 0:
        return None, REASON_NO_DATA
    return round(compliant / with_data * 100.0, 2), None
