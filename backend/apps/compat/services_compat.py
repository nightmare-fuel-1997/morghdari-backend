"""Compat services — slug resolvers + the flat-record translation (M6-09).

Everything the thin views need that is NOT HTTP and NOT the WS consumer:

- compat_resolve_org / compat_resolve_farm / compat_resolve_saloon:
  slug → (kind-row, entity-or-None). The org_id of the slug row MUST equal the
  caller's claim org (Security §4.1 branch ③) and the slug charset is
  [A-Za-z0-9-]{1,64} — anything else is a miss (→ 404 upstream, never 500).
  These reads run INSIDE the middleware's tenant transaction (GUC armed), so
  RLS is the backstop behind the explicit org filter.

- compat_flatten_reading: metrics-map → the exact Flutter keys of spec §6.
  Missing metric → "" (never null); clock_drift_ms from the DB-generated
  buffered_delay_mins (annotated by the views); seq stays "" (no per-row
  sequence exists in the hypertable — Flutter does value?.toString()).

- resolve_history_window: from_dt/to_dt (ISO-8601 or epoch-ms) → datetimes;
  inverted window → None (view maps to 400 invalid time window). limit
  default 100 (farm history 24), max 1000.

فارسی: خدمت‌های سازگاری — حلِ شناسهٔ قدیمی با کنترلِ سازمانِ ادعا، ترجمهٔ
رکوردِ تخت با کلیدهای دقیقِ فلاتر، و پنجرهٔ تاریخچه.
"""

import re
from datetime import datetime, timezone as dt_timezone

SLUG_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

FLUTTER_METRIC_NAMES = {
    "temp": "temperature",
    "humidity": "humidity",
    "lux": "lux",
    "nh3": "nh3",
}


def _claim_org_is(row_org_id, claims_org) -> bool:
    return str(row_org_id) == str(claims_org or "")


def _valid_slug(slug) -> bool:
    return bool(slug) and bool(SLUG_RE.match(str(slug)))


def compat_resolve_org(slug, claims_org):
    """Return (slug_row, org_entity_or_None) for kind='org' rows."""
    from apps.compat.models import LegacySlugMap
    from apps.tenants.models import Organization

    if not _valid_slug(slug):
        return None, None
    row = LegacySlugMap.objects.filter(slug=slug, kind="org").first()
    if row is None or not _claim_org_is(row.org_id, claims_org):
        return None, None
    return row, Organization.objects.filter(org_id=row.org_id).first()


def compat_resolve_farm(slug, claims_org):
    """Return (slug_row, farm_or_None) for kind='farm' rows."""
    from apps.compat.models import LegacySlugMap
    from apps.farms.models import Farm

    if not _valid_slug(slug):
        return None, None
    row = LegacySlugMap.objects.filter(slug=slug, kind="farm").first()
    if row is None or not _claim_org_is(row.org_id, claims_org):
        return None, None
    return row, Farm.objects.filter(org_id=row.org_id, pk=row.farm_id).first()


def compat_resolve_saloon(slug, claims_org):
    """Return (slug_row, saloon_or_None) for kind='device' rows (device==saloon)."""
    from apps.compat.models import LegacySlugMap
    from apps.farms.models import Saloon

    if not _valid_slug(slug):
        return None, None
    row = LegacySlugMap.objects.filter(slug=slug, kind="device").first()
    if row is None or not _claim_org_is(row.org_id, claims_org):
        return None, None
    return row, Saloon.objects.filter(org_id=row.org_id, pk=row.saloon_id).first()


def parse_iso_or_epoch_ms(raw):
    """ISO-8601 (incl. trailing Z) or epoch-ms → aware datetime, else None."""
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=dt_timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(raw.replace("Z", "+00:00") if raw.endswith("Z") else raw)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


def resolve_history_window(params, default_limit=100, max_limit=1000):
    """(since, until, limit, offset) or None on an inverted window."""
    since = parse_iso_or_epoch_ms(params.get("from_dt"))
    until = parse_iso_or_epoch_ms(params.get("to_dt"))
    if since is not None and until is not None and since > until:
        return None
    try:
        limit = int(params.get("limit") or default_limit)
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(params.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    return since, until, max(1, min(limit, max_limit)), max(0, offset)


def compat_flatten_reading(row, device_slug: str) -> dict:
    """One hypertable row (device_slug resolved upstream) → the exact Flutter
    flat record of spec §6."""
    metrics = row.metrics or {}
    flat = {flutter: "" for flutter in FLUTTER_METRIC_NAMES.values()}
    for backend_key, flutter_name in FLUTTER_METRIC_NAMES.items():
        window = metrics.get(backend_key)
        if isinstance(window, dict) and window.get("avg") is not None:
            flat[flutter_name] = str(window["avg"])

    return {
        "received_at": _iso(row.server_received_ts),
        "sent_ts": _iso(row.device_ts),
        "seq": "",
        "is_buffered": bool(row.is_buffered),
        "clock_drift_ms": _clock_drift_ms(row),
        **flat,
        "schema_version": "1",
        "device_slug": device_slug,
    }


def _clock_drift_ms(row) -> str:
    """buffered_delay_mins is a DB-GENERATED column (the ts delta) — computed
    here from the same delta instead of annotating, so the translation runs
    identically on rows, shim events and any backend. String stamps (shim
    events) are parsed before the delta; unparseable → "" (never a crash —
    a cosmetic field must never take down a push)."""
    delay = getattr(row, "buffered_delay_mins", None)
    if delay is not None:
        try:
            return str(int(round(float(delay) * 60000)))
        except (TypeError, ValueError):
            return ""
    received = _as_datetime(row.server_received_ts)
    sent = _as_datetime(row.device_ts)
    if received is None or sent is None:
        return ""
    return str(max(0, int(round((received - sent).total_seconds() * 1000))))


def _as_datetime(value):
    if isinstance(value, str):
        from django.utils.dateparse import parse_datetime

        try:
            value = parse_datetime(value.replace("Z", "+00:00") if value.endswith("Z") else value)
        except (ValueError, TypeError):
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)
    return None


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value  # fan-out events carry pre-formatted stamps
    text = value.isoformat()
    return text.replace("+00:00", "Z") if text.endswith("+00:00") else text


iso_z = _iso  # public alias — serializers/views share one timestamp shape


def flutter_metric_names(backend_keys) -> list:
    """allowed_metric_keys ∪ recent telemetry keys → Flutter capability names."""
    return sorted({
        FLUTTER_METRIC_NAMES[key]
        for key in backend_keys or []
        if key in FLUTTER_METRIC_NAMES
    })
