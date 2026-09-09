"""Compat URL routing — W1..W5 report aliases + the retained C16 WS shim
(M7-14 scoped removal).

M7-14 scoped removal deleted the C1..C15 REST routes (orgs/farms/devices/
telemetry). What remains:

Two prefixes hit the SAME W views (spec §3): the canonical
`/api/compat/v1/...` and the legacy Flutter `/api/...` aliases. No
redirects — minimal Flutter clients don't follow 301s on authed calls.
Trailing-slash tolerance is deliberate (the spec's "serve directly, no 301"
rule).

REST lives on the HTTP root; the C16 WS shim (apps.compat.consumers_compat,
retained untouched per the scoped-keep rule) mounts below on both prefixes
and is re-exported to config/routing.py.

فارسی: مسیرهای سازگاری — پیشوندِ جدید و نام‌های قدیمیِ فلاتر به همان ویوها.
"""

from django.urls import re_path

from apps.compat import views_compat as v
from apps.compat.consumers_compat import FarmTelemetryConsumer

FARM = r"(?P<farmSlug>[A-Za-z0-9-]{1,64})"
SALON = r"(?P<salonSlug>[A-Za-z0-9-]{1,64})"  # W1..W2 Flutter spelling (single l)

urlpatterns = [
    # W1..W5 — report aliases (M7-17, docs/api-reports-merge-v1.md §5).
    # Same two-prefix mounting as the removed C1..C15 (canonical
    # /api/compat/v1/... + legacy /api/... aliases); trailing-slash
    # tolerant, no 301s.
    re_path(rf"^farms/{FARM}/salons/{SALON}/reports/?$", v.compat_report_list),
    re_path(rf"^farms/{FARM}/salons/{SALON}/reports/(?P<report_date>[^/]+)/?$", v.compat_report_by_date),
    re_path(rf"^farms/{FARM}/reports/?$", v.compat_farm_report_list),
    re_path(r"^reports/?$", v.compat_report_create),
    re_path(r"^reports/(?P<report_id>[0-9]+)/?$", v.compat_report_edit),
]

# C16 — the farm WS shim, RETAINED (M7-14 scoped keeps the shim: the task's
# KEEP list names consumers_compat.py untouched, and the module gate FAILs
# any consumers*.py under 80% — an unmounted-but-retained consumer is dead
# code that reds the required full gate. Full C16 removal needs an explicit
# owner go AND the consumer file's deletion/exemption in the same commit).
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
