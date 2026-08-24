"""WSGI entrypoint — kept for compatibility only.

فارسی: فقط برای سازگاری نگه داشته شده؛ اجرای واقعی روی ASGI/Daphne است.
The runtime path is Daphne/ASGI (WebSocket is a first-class citizen here);
runserver also serves via daphne because "daphne" sits before staticfiles
in INSTALLED_APPS.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

application = get_wsgi_application()
