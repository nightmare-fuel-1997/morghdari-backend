"""Production settings — fail LOUD at boot, harden everything.

فارسی: تنظیمات production؛ نبودِ secretها باید همان اول با خطا متوقف شود.
Philosophy (M0-04 acceptance): a misconfigured prod deploy must crash at boot,
not silently serve with an insecure default. Every os.environ[...] here is a
deliberate KeyError-by-design.
"""

from .base import *  # noqa: F401,F403

DEBUG = False

# --- Fail-loud secrets -----------------------------------------------------
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]  # noqa: F405 — raises if missing
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()]  # noqa: F405
if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set in production")
if not os.environ.get("DATABASE_URL"):
    # Deployment hardening 2026-09-06: base.py ships NO database default —
    # an unset URL must abort the boot here (the old base default silently
    # aimed prod at dev credentials). Same KeyError-by-design philosophy.
    raise RuntimeError("DATABASE_URL must be set in production")

# --- TLS / transport hardening (Security Architecture §6) -------------------
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 63072000          # 2 years, includeSubDomains set at nginx
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# M7-04: the Prometheus scrape hits daphne directly on the internal network
# (plain HTTP — it cannot follow the redirect into a self-signed TLS port).
# M7-08: the internal prober vantage (blackbox-exporter-2 → daphne:8000) is
# the same class — a 301 there would make the burn-rate alerts measure the
# SSL redirect, not availability. Exempt ONLY machine paths; nginx still
# 404s them publicly (or they're doc'd API), so no human-facing surface is
# affected. Everything else keeps the redirect.
SECURE_REDIRECT_EXEMPT = ["api/v1/metrics/", "api/v1/health/"]

# --- Logging: JSON to stdout — Loki/Promtail scrapes container logs --------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            # Observability §7.1: structured logs carry service/instance/trace_id
            "format": '{"ts":"%(asctime)s","level":"%(levelname)s",'
                      '"logger":"%(name)s","msg":"%(message)s"}',
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
