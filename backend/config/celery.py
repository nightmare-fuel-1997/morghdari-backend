"""Celery application — Blueprint §5 canonical topology.

Queues (drift is over): telemetry_realtime, telemetry_historical, alerts,
audit, provisioning. Routing lives in settings.CELERY_TASK_ROUTES; the
historical task MUST be listed BEFORE the telemetry.* wildcard.
فارسی: اپ Celery با صف‌های کانونیکل؛ مسیریابی در تنظیمات و پیش از وایلدکارد.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("morghdari")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
