"""Celery application — single app instance for worker AND beat.

فارسی: اپلیکیشن Celery؛ هم worker و هم beat از همین نمونه استفاده می‌کنند.
Blueprint §1: `celery -A config worker` / `celery -A config beat`. Task
autodiscovery scans apps/*/tasks.py so each domain keeps its own tasks.
Queue routing lives in settings.base.CELERY_TASK_ROUTES (canonical names,
Blueprint §5 — drift in task/queue naming was a repeated audit finding).
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("morghdari")

# All config comes from Django settings with the CELERY_ namespace prefix;
# no second config file to keep in sync (Configuration-is-data, Decision 21).
app.config_from_object("django.conf:settings", namespace="CELERY")

# Discover tasks in every installed app (apps.<domain>.tasks modules).
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_ping(self):
    """Trivial smoke task used by dev checks; never routed in production flows."""
    return "pong"
