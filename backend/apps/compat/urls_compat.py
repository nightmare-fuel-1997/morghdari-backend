"""Compat URL routing — C1..C16 (M6-09).

Two prefixes hit the SAME views (spec §3): the canonical `/api/compat/v1/...`
and the legacy Flutter `/api/...` aliases. No redirects — minimal Flutter
clients don't follow 301s on authed calls. Trailing-slash tolerance is
deliberate (the spec's "serve directly, no 301" rule).

REST lives on the HTTP root; the WS shim mounts in config/routing.py (the
ProtocolTypeRouter's websocket leg) on both prefixes.

فارسی: مسیرهای سازگاری — پیشوندِ جدید و نام‌های قدیمیِ فلاتر به همان ویوها.
"""

from django.urls import path, re_path

from apps.compat import views_compat as v
from apps.compat.consumers_compat import FarmTelemetryConsumer

ORG = r"(?P<orgSlug>[A-Za-z0-9-]{1,64})"
FARM = r"(?P<farmSlug>[A-Za-z0-9-]{1,64})"
DEVICE = r"(?P<deviceSlug>[A-Za-z0-9-]{1,64})"
SALON = r"(?P<salonSlug>[A-Za-z0-9-]{1,64})"  # W1..W2 Flutter spelling (single l)

urlpatterns = [
    # C1..C4 — orgs (list routes match BOTH slash forms directly — APPEND_SLASH
    # would 301, and minimal Flutter clients don't follow redirects: spec §4)
    re_path(rf"^orgs/{ORG}/?$", v.org_detail),
    re_path(r"^orgs/?$", v.orgs_list),
    re_path(rf"^orgs/{ORG}/farms/?$", v.org_farms),
    re_path(rf"^orgs/{ORG}/devices/?$", v.org_devices),
    # C5..C7 — farms
    re_path(r"^farms/?$", v.farms_list),
    re_path(rf"^farms/{FARM}/?$", v.farm_detail),
    re_path(rf"^farms/{FARM}/devices/?$", v.farm_devices),
    # C8..C9 — devices (== saloons)
    re_path(r"^devices/?$", v.devices_list),
    re_path(rf"^devices/{DEVICE}/?$", v.device_detail),
    # C10..C15 — telemetry flat records
    re_path(rf"^telemetry/orgs/{ORG}/latest/?$", v.telemetry_org_latest),
    re_path(rf"^telemetry/orgs/{ORG}/history/?$", v.telemetry_org_history),
    re_path(rf"^telemetry/farms/{FARM}/latest/?$", v.telemetry_farm_latest),
    re_path(rf"^telemetry/farms/{FARM}/history/?$", v.telemetry_farm_history),
    re_path(rf"^telemetry/devices/{DEVICE}/latest/?$", v.telemetry_device_latest),
    re_path(rf"^telemetry/devices/{DEVICE}/history/?$", v.telemetry_device_history),
    # W1..W5 — report aliases (M7-17, docs/api-reports-merge-v1.md §5).
    # Same two-prefix mounting as C1..C15 (canonical /api/compat/v1/... +
    # legacy /api/... aliases); trailing-slash tolerant, no 301s.
    re_path(rf"^farms/{FARM}/salons/{SALON}/reports/?$", v.compat_report_list),
    re_path(rf"^farms/{FARM}/salons/{SALON}/reports/(?P<report_date>[^/]+)/?$", v.compat_report_by_date),
    re_path(rf"^farms/{FARM}/reports/?$", v.compat_farm_report_list),
    re_path(r"^reports/?$", v.compat_report_create),
    re_path(r"^reports/(?P<report_id>[0-9]+)/?$", v.compat_report_edit),
]

# C16 — the farm WS shim, canonical + legacy path (same consumer)
websocket_urlpatterns = [
    re_path(
        rf"^api/compat/v1/ws/farms/{FARM}/telemetry$",
        FarmTelemetryConsumer.as_asgi(),
    ),
    re_path(
        rf"^ws/farms/{FARM}/telemetry$",
        FarmTelemetryConsumer.as_asgi(),
    ),
]
