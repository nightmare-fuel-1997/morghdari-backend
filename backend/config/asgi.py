"""ASGI entrypoint — serves HTTP AND WebSocket (Daphne).

فارسی: ورودی ASGI؛ هم HTTP و هم WebSocket را سرو می‌کند.
Channels' ProtocolTypeRouter is the single dispatcher; websocket URL routing
arrives in config/routing.py at v0.6. Daphne is started against this app in
the production compose (`daphne -b 0.0.0.0 -p 8000 config.asgi:application`).
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

django_asgi_app = get_asgi_application()

application = django_asgi_app  # v0.6 wraps this in ProtocolTypeRouter(websocket=routing)
