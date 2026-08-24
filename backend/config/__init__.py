"""Celery loader hook — makes `celery -A config` work.

فارسی: هوک لازم تا دستور `celery -A config` اپ را پیدا کند.
"""

from .celery import app as celery_app

__all__ = ("celery_app",)
