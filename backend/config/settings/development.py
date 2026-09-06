"""Development settings — comfort features, zero hardening.

فارسی: تنظیمات توسعه؛ راحتی بالا، سخت‌گیری امنیتی فقط برای محلی.
"""

import importlib.util
import os

# Laptop default BEFORE base import (the razor: comfort belongs to the
# comfort module) — mirrors docker-compose.dev.yml credentials exactly.
# Deployment hardening 2026-09-06: base.py ships NO default, production
# fail-fasts — a bare env must never aim prod at dev credentials.
os.environ.setdefault(
    "DATABASE_URL",
    "postgres://morghdari_dev:dev_password_only@localhost:5432/morghdari_dev",
)

from .base import *  # noqa: F401,F403 — the shared truth

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Debug toolbar loads ONLY if actually installed (keeps bare checkouts green).
if importlib.util.find_spec("debug_toolbar"):
    import debug_toolbar  # noqa: F401

    INTERNAL_IPS = ["127.0.0.1"]
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")  # noqa: F405
    INSTALLED_APPS += ["debug_toolbar", "django_extensions"]  # noqa: F405

# NOTE on CORS for owner-console: deliberately NOT wired yet. When the console
# starts calling real APIs (v0.3+), either add django-cors-headers here or serve
# the console through Django — decided at that version, not pre-solved now.
