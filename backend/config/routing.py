"""WebSocket URL routing — live since v0.6 (M6-06).

فارسی: مسیرهای وب‌سوکت؛ از v0.6 زنده — سوکتِ زندهٔ داشبورد برای هر سالن.
The consumer pattern (JWT from ?token=, org-membership check, ws:active cap)
is specified in Blueprint §6 / Security §4.4 and lives in apps/telemetry
(ws_consumer.SaloonTelemetryConsumer). The v0.6 compat farm shim
(consumers_compat.py, M6-09) mounts its own /ws/farms/... path later.
"""

from django.urls import re_path

from apps.compat.urls_compat import websocket_urlpatterns as compat_websocket_urlpatterns
from apps.telemetry.ws_consumer import SaloonTelemetryConsumer

websocket_urlpatterns = [
    # ?token=<JWT> — the credential rides the query string (C16: never a
    # header on WS; browsers cannot set headers on the WebSocket handshake).
    re_path(
        r"^ws/saloons/(?P<saloon_id>\d+)/telemetry$",
        SaloonTelemetryConsumer.as_asgi(),
    ),
    # M6-09 compat shim: /api/compat/v1/ws/farms/... + legacy /ws/farms/...
    *compat_websocket_urlpatterns,
]
