"""Compat serializers — exact Flutter key names (M6-09, spec §5/§6).

Read-only dict builders (no DRF Serializer machinery — these are stable
projections of frozen tables, and every key is spelled exactly as the shipped
Flutter `fromJson` factories read it). The flat telemetry record lives in
services_compat.compat_flatten_reading; this module holds org/farm/device.

Device-item derivations (spec §5 table): status from the saloon's gateway
zone + the gateway:status:{slot} Redis key (reusing the farms/api helpers —
additive import, zero edits to that app); last_seen_at = max
server_received_ts; sensor_capabilities = allowed_metric_keys of ACTIVE
slots ∪ keys seen in the saloon's most recent reading, mapped to Flutter
names; firmware_version always "" (no such column — Flutter hides the tile).

فارسی: سریال‌سازِ سازگاری — کلیدها دقیقاً همان‌هایی که فلاتر می‌خواند.
"""


def org_item(slug_row, org) -> dict:
    return {
        "slug": slug_row.slug,
        "name": org.name if org else "",
        "created_at": _iso(org.created_at if org else None),
    }


def farm_item(slug_row, farm, org_slug_row) -> dict:
    return {
        "slug": slug_row.slug,
        "name": farm.name if farm else "",
        "organization_slug": org_slug_row.slug if org_slug_row else "",
        "created_at": _iso(farm.created_at if farm else None),
    }


def device_item(slug_row, saloon, *, farm_slug, org_slug, status, last_seen_at, capabilities, created_at) -> dict:
    """The §5 shape — both org_* keys returned (Flutter reads both)."""
    return {
        "slug": slug_row.slug,
        "name": saloon.name if saloon else "",
        "status": status,
        "firmware_version": "",
        "sensor_capabilities": capabilities,
        "last_seen_at": last_seen_at,
        "farm_slug": farm_slug,
        "org_slug": org_slug,
        "organization_slug": org_slug,
        "created_at": created_at,
    }


def _iso(value) -> str:
    if value is None:
        return ""
    text = value.isoformat()
    return text.replace("+00:00", "Z") if text.endswith("+00:00") else text
