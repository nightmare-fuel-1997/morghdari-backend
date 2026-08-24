"""
Base settings — the shared truth every environment module builds on.

فارسی: تنظیمات پایه؛ همهٔ محیط‌ها (dev/test/prod) روی همین بنا می‌شوند.

Design rules enforced here (Implementation Blueprint §2):
1. ONE env-var convention: DATABASE_URL and REDIS_URL carry everything
   (credentials included). The discrete HOST/NAME style is banned — the R3-fix
   killed it because prod compose, CI compose and settings must speak one dialect.
2. Redis logical DBs are separated by NUMBER, not server: broker=db0,
   cache=db1, channels=db2 — flushing the cache can never eat a task queue.
3. CELERY_TIMEZONE is Asia/Tehran: "midnight" crons (threshold cache) are
   farm-local events; in UTC they would fire at 03:30 local (Audit AU-TZ).
4. bcrypt joins PASSWORD_HASHERS from day one: mqtt_device_credentials hashes
   MUST be verifiable by EMQX's password_based:bcrypt backend (database.md C7),
   and Django's default pbkdf2 strings are NOT parseable by EMQX.
"""

import os
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Secrets / debug flags — overridden per environment module
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = False
ALLOWED_HOSTS: list[str] = []

# ---------------------------------------------------------------------------
# DATABASE — minimal DATABASE_URL parser (razor rule)
# ---------------------------------------------------------------------------
# فارسی: پارسر کوچک DATABASE_URL به‌جای افزودن کتابخانهٔ اضافه (قاعدهٔ تیغ).
def _database_from_url(url: str) -> dict:
    """Translate postgres://user:pass@host:port/dbname into Django DATABASES.

    A 10-line parser beats an extra dependency (django-environ) at this scale;
    the URL dialect is emitted identically by prod compose, CI compose and docs.
    """
    parsed = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "localhost",
        "PORT": str(parsed.port or 5432),
    }


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    # Local dev default mirrors docker-compose.dev.yml credentials exactly.
    "postgres://morghdari_dev:dev_password_only@localhost:5432/morghdari_dev",
)
DATABASES = {"default": _database_from_url(DATABASE_URL)}


def _argon2_installed() -> bool:
    """Argon2 is optional (not pinned); fall back to PBKDF2 silently."""
    try:
        import argon2  # noqa: F401

        return True
    except ImportError:
        return False

# Connection reuse: persistent conns keep the 100-connection budget predictable
# (Deployment Guide §12). Each worker holds ONE conn per thread, not per request.
CONN_MAX_AGE = 60

# ---------------------------------------------------------------------------
# REDIS — single URL, three logical DBs
# ---------------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL + "/1",  # db1 = cache (thresholds, zones, ws:active)
    }
}

# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "daphne",  # ASGI dev server; must precede staticfiles (channels convention)
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "django_celery_beat",
    # Project apps — one per domain, Blueprint §1 enumeration.
    # Model homes are decided ONCE here; never invent extra apps (M0-05).
    "apps.accounts",
    "apps.tenants",
    "apps.geography",
    "apps.farms",
    "apps.devices",
    "apps.telemetry",
    "apps.alerts",
    "apps.mqtt_auth",
    "apps.provisioning",
    "apps.auditing",
    "apps.system_config",
    "apps.monitoring",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # TenantOrgMiddleware (v0.2) will be appended here — it must run AFTER auth.
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Password validation (Django defaults — fine for the human side; device
# credentials are 32-char random tokens generated server-side, not user passwords)
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# bcrypt MUST be present for mqtt_device_credentials (see module docstring #4).
# Argon2/PBKDF2 stay first so HUMAN passwords keep Django's default strength;
# EMQX only ever reads the MQTT table, which our code hashes with bcrypt directly.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher" if _argon2_installed() else "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

# ---------------------------------------------------------------------------
# i18n / static
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Tehran"          # farm-local time; USE_TZ=True keeps DB in UTC
USE_I18N = True
USE_TZ = True                       # dual-timestamp audit math depends on this

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# REST framework — minimal for v0.1; auth classes arrive with JWT at v0.2
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],  # deliberately empty until JWT (v0.2)
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

# ---------------------------------------------------------------------------
# Celery — broker db0; beat uses the DatabaseScheduler (django-celery-beat)
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = REDIS_URL + "/0"
CELERY_RESULT_BACKEND = REDIS_URL + "/0"
CELERY_TIMEZONE = "Asia/Tehran"     # AU-TZ: 'midnight' crons are farm-local
CELERY_TASK_ROUTES = {
    # Canonical queue names (Blueprint §5). Tasks themselves land in v0.3/v0.4;
    # declaring routes now prevents accidental default-queue drift later.
    "telemetry.*": {"queue": "telemetry_realtime"},
    "alerts.*": {"queue": "alerts"},
    "auditing.*": {"queue": "audit"},
    "provisioning.*": {"queue": "provisioning"},
}
# Historical lane is throttled IN THE WORKER from system_configuration (Saas §4);
# Celery-level rates are intentionally NOT set here to avoid two sources of truth.

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL + "/2"]},  # db2 = channels (never the broker db)
    }
}


