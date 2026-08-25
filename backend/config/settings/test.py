"""Test settings — fast, hermetic, honest.

فارسی: تنظیمات تست؛ سریع و ایزوله. تست RLS استثناست و مهاجرت‌ها را لازم دارد.

Three speed levers (Blueprint §2/§7):
1. MD5 password hasher — hashing dominates user-creation time in tests.
2. CELERY_TASK_ALWAYS_EAGER — tasks run inline; no broker needed for unit tests.
3. DisableMigrations + --nomigrations — tables come from CURRENT MODEL STATE,
   not migration history.

The ONE exception: tests/test_rls.py runs migrations-enabled, because the
policies created by initdb/02_rls.sql / the RLS migration would never exist
under --nomigrations and cross-org assertions would pass VACUOUSLY (Blueprint §7).

DATABASE_URL present (CI) → real PostgreSQL/TimescaleDB.
DATABASE_URL absent (laptop quick loop) → in-memory sqlite. Anything that only
works on sqlite is lying about production; integration truth lives in CI.
"""

import os

from .base import *  # noqa: F401,F403
# Star-import skips underscore names (Python semantics), and the postgres
# branch below needs the URL parser — first proof: CI caught this on its
# very first run while local sqlite runs never executed that branch.
from .base import _database_from_url  # noqa: F401

DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "test"]

# 32+ bytes so SimpleJWT's HS256 signing stops raising InsecureKeyLengthWarning
# (RFC 7518 §3.2 minimum); still never a secret: test env by definition.
# فارسی: کلید تست حداقل ۳۲ بایت است تا هشدار طول کلید JWT حذف شود؛ باز هم محرمانه نیست.
SECRET_KEY = "test-only-insecure-key-0123456789abcdef!"  # never a secret: test env by definition

# Speed lever 1: fastest hasher allowed by Django's validator chain.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # noqa: F405

# Speed lever 2: inline task execution.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_EAGER_PROPAGATES = True  # a failing task must FAIL the test, not vanish


class DisableMigrations:
    """MIGRATION_MODULES replacement: app-label → None means 'no migrations'.

    Django then creates tables directly from current model state.
    فارسی: به‌جای تاریخچهٔ مهاجرت، جدول‌ها مستقیم از مدل‌های فعلی ساخته می‌شوند.
    """

    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = DisableMigrations()

if os.environ.get("DATABASE_URL"):
    DATABASES = {"default": _database_from_url(os.environ["DATABASE_URL"])}  # noqa: F405
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
