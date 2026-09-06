"""ASGI entrypoint — serves HTTP AND WebSocket (Daphne).

فارسی: ورودی ASGI؛ هم HTTP و هم WebSocket را سرو می‌کند.
Channels' ProtocolTypeRouter is the single dispatcher: http → the Django ASGI
app (middleware, GUC, everything as before), websocket → config/routing.py
(the saloon telemetry consumer since M6-06). Daphne is started against this
app in the production compose (`daphne -b 0.0.0.0 -p 8000 config.asgi:application`).
"""

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

django_asgi_app = get_asgi_application()

from config.routing import websocket_urlpatterns  # noqa: E402  (after django setup)

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": URLRouter(websocket_urlpatterns),
    }
)
