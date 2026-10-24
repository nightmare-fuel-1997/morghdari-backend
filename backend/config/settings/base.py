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
from datetime import timedelta
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
    "DATABASE_URL"
)  # NO default (deployment hardening 2026-09-06): a missing URL must fail at
# boot, never silently aim prod at a dev database with dev credentials.
# Laptop default lives in development.py (seeded before this import);
# production.py raises RuntimeError when unset; test.py maps absent → sqlite.
DATABASES = (
    {"default": _database_from_url(DATABASE_URL)} if DATABASE_URL else {}
)

# M7-08 (MET-SAAS): the nightly org rollup crosses tenants BY DESIGN, but the
# runtime connection is morghdari_app — FORCE-RLS fail-closed on purpose. The
# doc-sanctioned bypass is the admin role (BYPASSRLS; the same role
# scripts/prod_bootstrap.sh uses for platform paths), wired as an OPTIONAL
# second alias: prod compose sets DATABASE_ADMIN_URL on the worker + daphne
# (BUG-11: daphne needs it too — see below), CI/laptop leave it unset (their
# connect user is the DB superuser already) and the task falls back to
# "default". Never used by request traffic EXCEPT the BUG-11 device-config
# discovery: the gateway_config endpoint IS request traffic but can carry no
# org context (anonymous gateway Basic + slot lookup pre-auth), so DISCOVERY
# runs on the admin alias while authorization stays in-view and every
# per-tenant read/write stays on "default" inside tenant_context.
# Discovery-only use.
# فارسی (BUG-11): «هرگز در ترافیک درخواستی استفاده نمی‌شود» یک استثنا دارد —
# کشف device-config در gateway_config: این اندپوینت ترافیک درخواستی است اما
# هیچ زمینهٔ سازمانی نمی‌تواند داشته باشد (Basic گیت‌وی + جست‌وجوی اسلات پیش
# از احراز هویت)؛ فقط «کشف» روی alias ادمین انجام می‌شود، مجوزدهی داخل ویو
# می‌ماند و خواندن/نوشتن هر سازمان همچنان روی پیش‌فرض داخل tenant_context است.
DATABASE_ADMIN_URL = os.environ.get("DATABASE_ADMIN_URL")
if DATABASE_ADMIN_URL:
    DATABASES["admin"] = _database_from_url(DATABASE_ADMIN_URL)


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
    "drf_spectacular",
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
    "apps.reports",
    # M6-09: additive Flutter compat layer (legacy_slug_map + /api/compat/v1).
    "apps.compat",
]

AUTH_USER_MODEL = "accounts.User"  # C-09: doc's extended auth_user (is_platform_staff)

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # v0.2 (D2/D3): verify-once tenant middleware. MUST stay AFTER
    # AuthenticationMiddleware — order is asserted in test_m2_05.
    "apps.tenants.middleware.TenantOrgMiddleware",
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

# M6-10 (O6, contract v1.7.0): photo uploads allow ≤10 MB (spec §2), so the
# request-body ceiling must sit above that — Django's default 2.5 MB would
# reject legal O6 uploads before the view's own size check runs. Additive:
# only raised for uploads; nothing else posts large bodies.
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
MEDIA_ROOT = BASE_DIR.parent / "media"  # backend/media (local storage backend)
MEDIA_URL = "/media/"

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

STATIC_URL = "/static/"
# PROD-FIX 2026-09-19 (admin CSS 404, live on VPS 82.115.17.252): STATIC_ROOT
# must be /app/static, the path docker-compose.prod.yml mounts static_volume
# on (and nginx serves from /static). It was BASE_DIR/"staticfiles"
# (/app/config/staticfiles) — an ephemeral container path nothing served, so
# the volume stayed empty and every /static/admin/* answered 404. Same
# BASE_DIR.parent pattern as MEDIA_ROOT.
STATIC_ROOT = BASE_DIR.parent / "static"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# REST framework — auth arrives with JWT at v0.2 (Security §4.1, decision D3):
# CachedJWTAuthentication reuses the middleware's verify-once stash when
# TenantOrgMiddleware has run, else verifies stock. Permission stays deny-all.
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.CachedJWTAuthentication",
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # Auto-schema powers /api/schema/ + Swagger UI; endpoints self-document
    # from v0.2 onward (owner-requested docs page at v0.1 close).
     "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# M6-10 (O10): the ONE strict throttle lane — deliberately NO default
# throttle (v1.5.0 paths stay untouched; nginx owns coarse limits).
# auth_strict = 3/min per user, wired ONLY to the password-change view.
# BUG-5 (QA 1.26): auth_login = 60/min per attempted username, wired ONLY to
# the login view — generous on purpose: the densest suite module does 43 total
# logins (densest single username 13), so 60 never false-triggers legit bursts.
# فارسی: نرخِ ورود ۶۰ در دقیقه برای هر نامِ کاربری؛ عمداً گشاده تا آزمون‌ها خطا ندهند.
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {"auth_strict": "3/min", "auth_login": "60/min"}

# ---------------------------------------------------------------------------
# JWT (SimpleJWT) — Security Architecture §3.2; owner decision Tracker §4i.
# Lifetimes: access 15 min / refresh 7 days. Rotation + blacklist are OFF
# FOREVER (owner decision 2026-08-23): offboarding relies on claim semantics,
# not revocation — a deactivated member's fresh tokens carry no org_id so RLS
# fails closed (Security §4.1b, constraint C-14). The token_blacklist app is
# therefore NEVER installed: registering it would promise revocation we do
# not implement in v0.x.
# فارسی: عمر توکن دسترسی ۱۵ دقیقه و توکن نوسازی ۷ روز؛ چرخش و سیاههٔ توکن برای
# همیشه خاموش است (تصمیم مالک). خروجِ عضو از راهِ حذف ادعای org_id و RLS کنترل
# می‌شود، نه ابطال توکن؛ پس اپ token_blacklist هرگز ثبت نمی‌شود.
# ---------------------------------------------------------------------------
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# ---------------------------------------------------------------------------
# OpenAPI documentation (drf-spectacular) — served at /api/schema/ + UIs.
# فارسی: صفحهٔ مستندات API؛ از همین نسخه سرو می‌شود تا نسخه‌های بعد خودکار
# مستندسازی شوند.
# ---------------------------------------------------------------------------
SPECTACULAR_SETTINGS = {
    "TITLE": "Morghdari Poultry Monitoring API",
    "DESCRIPTION": (
        "REST API for the Morghdari multi-tenant poultry monitoring platform. "
        "Contract v1.7.8 (ops wave + merged daily-report schemas + the "
        "threshold-template write surface O13–O16 + the follow-through core + "
        "the Hardware Team write path H0–H4 + alert-subscription mutes B2 + "
        "granular panels B3 + public status page B4 + reconciliation B5).\n\n"
        "Authentication: Bearer JWT (15-min access token, 7-day refresh token; "
        "rotation is intentionally OFF — deactivated members fail closed via "
        "org-claim re-derivation). Gateway devices use HTTP Basic "
        "(username = gateway_slot_id) on the config endpoint only.\n\n"
        "Tenancy: every response is scoped to the caller's org_id claim; "
        "cross-org ids answer 404 (never distinguished from unknown ids). "
        "Roles: owner > manager > engineer > doctor/worker/installer; "
        "reviewer is read-only on daily reports.\n\n"
        "Base URL: https://api.morghdari.com/api/v1/ — the canonical "
        "human-written contract is backend/openapi.yaml (the "
        "implemented == contract gate); this page is its generated mirror.\n\n"
        "WebSocket live docs (Swagger covers REST only): /api/ws-docs/ "
        "(human page with JS/Dart snippets) + /api/ws-schema/ (machine JSON)."
    ),
    "VERSION": "1.7.8",
    "CONTACT": {"name": "Morghdari Platform Team"},
    "LICENSE": {"name": "Proprietary — Morghdari"},
    # 2026-09-11 fix (Swagger "Try it out" NetworkError): the server is a
    # bare relative "/" — never a hardcoded host. drf-spectacular emits full
    # operation paths (/api/v1/...), so Swagger UI concatenates server + path
    # and Executes same-origin on EVERY door (:8000 dev, :443 rehearsal, the
    # VPS). A hardcoded https://api.morghdari.com first entry made every
    # Execute POST to a host that only resolves in production — the browser
    # failed the fetch (TypeError: NetworkError) and the response panel read
    # "(Undocumented)" because nothing ever arrived. The production base URL
    # stays documented in DESCRIPTION text above.
    "SERVERS": [{"url": "/", "description": "This server — requests resolve against the host serving these docs"}],
    "SERVE_INCLUDE_SCHEMA": False,
    # Docs pages are public read-only even though the API itself is not:
    # a contract you cannot see cannot be integrated against.
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    # Every endpoint is Bearer-gated unless its @extend_schema says auth=[].
    # The scheme itself comes from CachedJWTAuthenticationExtension
    # (apps/accounts/authentication.py); GatewayBasicAuth is declared here
    # for the dual-auth gateway config endpoint.
    "SECURITY": [{"BearerAuth": []}],
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "GatewayBasicAuth": {
                "type": "http",
                "scheme": "basic",
                "description": (
                    "Gateway device authentication for the zero-touch config "
                    "endpoint. Username = gateway_slot_id, password = the "
                    "current MQTT credential."
                ),
            }
        }
    },
    "TAGS": [
        {"name": "Authentication", "description": "Login, token refresh, MFA, sessions."},
        {"name": "Org Management", "description": "Owner-exclusive member invite/deactivate."},
        {"name": "Farms & Saloons", "description": "Farm list, saloon detail, operational-state loop, flock cycles."},
        {"name": "Telemetry", "description": "Saloon/farm sensor reads, hourly analytics, metric catalog."},
        {"name": "Provisioning & Gateways", "description": "Geography drill-down, gateway requests/review, lifecycle, config, rotation."},
        {"name": "Alerts & KPI", "description": "Saloon alerts + ack, weight/feed/water intake, biosecurity, capabilities, KPIs, dashboard config."},
        {"name": "Monitoring", "description": "Public health + hardware-time probes."},
        {"name": "Ops", "description": "Audit logs, thresholds, photos, gateway reads, password change, sessions."},
        {"name": "Reports", "description": "Daily saloon reports (DRAFT→SUBMITTED→APPROVED/REJECTED + FLAGGED), vet review."},
        {"name": "Platform Support", "description": "Staff-scoped support session + whitelisted ops (audited)."},
        {"name": "Compat", "description": "Flutter compat aliases under /api/compat/v1 (W1–W5 report routes)."},
    ],
    "COMPONENT_SPLIT_REQUEST": True,
    # Drop the legacy /api/ compat twins from the DOCS only (they share view
    # functions with /api/compat/v1, so per-view exclude would kill both).
    # add_metrics_scrape_path: /api/v1/metrics/ is a plain Django view
    # (text/plain scrape — not @api_view) so spectacular can't introspect it.
    "POSTPROCESSING_HOOKS": [
        "apps.common.schema_hooks.drop_legacy_compat_aliases",
        "apps.common.schema_hooks.add_metrics_scrape_path",
    ],
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
    # SPECIFIC routes must precede the telemetry.* wildcard (D-6 fix): the
    # historical task rides its own lane so replay storms never touch realtime.
    "telemetry.process_historical_payload": {"queue": "telemetry_historical"},
    "telemetry.flush_telemetry_buffer": {"queue": "telemetry_realtime"},
    "telemetry.*": {"queue": "telemetry_realtime"},
    "alerts.*": {"queue": "alerts"},
    "auditing.*": {"queue": "audit"},
    "provisioning.*": {"queue": "provisioning"},
}
# Drain tasks are BEAT-DRIVEN (one cycle per kick, 10s): stateless workers,
# crash-simple self-healing. No self-rescheduling recursion to reason about.
CELERY_BEAT_SCHEDULE = {
    "alerts-daily-thresholds": {
        "task": "alerts.cache_daily_thresholds",
        "schedule": __import__("celery.schedules", fromlist=["crontab"]).crontab(hour=0, minute=0),  # Asia/Tehran via CELERY_TIMEZONE
        "options": {"queue": "alerts"},
    },
    "telemetry-historical-drain-kick": {
        "task": "telemetry.process_historical_payload",
        "schedule": 10.0,
        "options": {"queue": "telemetry_historical"},
    },
    "telemetry-realtime-drain-kick": {
        "task": "telemetry.process_realtime_payload",
        # §4l-Q7: 10s -> 2s so the Tier-1 "<2 s" exit-demo claim holds end to
        # end (settings-only change per the §4k-Q5 precedent). The historical
        # lane deliberately keeps its 10s kick.
        "schedule": 2.0,
        "options": {"queue": "telemetry_realtime"},
    },
    # M4-05 escalation heartbeat: Tier 2 at 15 min / Tier 3 at 30 min of
    # unresolved breach; sweep cadence 2 min keeps worst-case promotion lag
    # inside the tier window (TUTORIAL §3.4).
    "alerts-escalation-sweep": {
        "task": "alerts.escalate_alerts",
        "schedule": 120.0,
        "options": {"queue": "alerts"},
    },
    # M4-07 LOS heartbeat: 60 s sweep; the 300 s grace lives in the
    # sensor:status TTL (consumer-written on every live reading). Fully
    # suppressed after a Redis restart for los_suppress_after_redis_restart_min
    # (DR T3 — keys repopulate slower than the sweep runs). Blueprint §5.
    "alerts-los-watchdog": {
        "task": "alerts.los_watchdog_sweep",
        "schedule": 60.0,
        "options": {"queue": "alerts"},
    },
    # M7-04 observability: beat liveness for the BeatHeartbeatStale rule
    # (monitoring/alerts.yml — 300 s staleness window). The task writes a
    # Redis timestamp; daphne's /metrics/ export lifts it into the gauge.
    "monitoring-beat-heartbeat": {
        "task": "monitoring.beat_heartbeat",
        "schedule": 60.0,
        "options": {"queue": "alerts"},
    },
    # AD-3 (2026-10-18): configuration convergence. Every slot whose device has
    # not reported the current `desired_config_version` is republished — this is
    # the ENTIRE retry story for retained desired state, replacing per-command
    # retry queues. 60 s keeps a broker outage invisible to the installer.
    "provisioning-config-convergence-sweep": {
        "task": "provisioning.sweep_config_convergence",
        "schedule": 60.0,
        "options": {"queue": "celery"},
    },
    # Phase 3 / task 3.4: a SENT command that the device never answered has
    # nothing to re-drive it. Without this sweep it sits in SENT forever and the
    # UI shows a command that will never complete. 60 s keeps the displayed
    # state honest without hammering the table.
    "provisioning-command-expiry-sweep": {
        "task": "provisioning.sweep_command_expiry",
        "schedule": 60.0,
        "options": {"queue": "celery"},
    },
    # M7-08 MET-SAAS: the doc's nightly per-org rollup (data-aggregates-v1
    # `aggregates.build_org_usage` — "nightly 01:10") + the §5.4 daily
    # tenant_cost_estimate_usd export. Runs with the default queue: once a
    # day, no latency contract.
    "monitoring-build-org-usage": {
        "task": "monitoring.build_org_usage",
        "schedule": __import__("celery.schedules", fromlist=["crontab"]).crontab(hour=1, minute=10),  # Asia/Tehran via CELERY_TIMEZONE
        "options": {"queue": "celery"},
    },
    # v0.9 M9-10 (data-aggregates-v1 §4): batch-1 daily rollups, 00:30–01:00
    # Asia/Tehran, AFTER the midnight threshold cron (00:00) they read bands
    # from. Default queue: once a day each, no latency contract.
    # 2026-10-24: the HOURLY writer metric_hourly_aggregates never had — the
    # daily rollup reads hourly rows, so without this it aggregated an empty
    # table forever. 30-minute trailing-window upsert keeps charts fresh and
    # self-heals late buffered rows. Default queue: cheap, no latency contract.
    "aggregates-build-metric-hourly": {
        "task": "aggregates.build_metric_hourly",
        "schedule": 1800.0,
        "options": {"queue": "celery"},
    },
    "aggregates-build-metric-daily": {
        "task": "aggregates.build_metric_daily",
        "schedule": __import__("celery.schedules", fromlist=["crontab"]).crontab(hour=0, minute=30),
        "options": {"queue": "celery"},
    },
    "aggregates-build-flock-snapshots": {
        "task": "aggregates.build_flock_snapshots",
        "schedule": __import__("celery.schedules", fromlist=["crontab"]).crontab(hour=0, minute=40),
        "options": {"queue": "celery"},
    },
    "aggregates-build-alert-stats": {
        "task": "aggregates.build_alert_stats",
        "schedule": __import__("celery.schedules", fromlist=["crontab"]).crontab(hour=0, minute=50),
        "options": {"queue": "celery"},
    },
    "aggregates-build-gateway-uptime": {
        "task": "aggregates.build_gateway_uptime",
        "schedule": __import__("celery.schedules", fromlist=["crontab"]).crontab(hour=1, minute=0),
        "options": {"queue": "celery"},
    },
}
# Historical lane is throttled IN THE WORKER from system_configuration (Saas §4);
# Celery-level rates are intentionally NOT set here to avoid two sources of truth.

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        # BUG-8 (QA 15.8): bound the async layer's CONNECT — group_add on a
        # dead redis must raise fast (the consumer degrades to no-push), never
        # hold the WS handshake past Daphne's ~5s guillotine (live nginx 502).
        # Extra host-dict keys flow into the redis ConnectionPool (see
        # channels_redis.utils.create_pool). db2 = channels (never broker db).
        #
        # BUG-22 (QA 15.9): NO socket_timeout here — channels_redis receive()
        # is a brpop_timeout=5 long-poll (core.py); a 1s read timeout raised
        # into the ASGI app and Daphne closed every otherwise-idle WS ~2s
        # after connect (live with redis UP: WSCONNECT→WSDISCONNECT 2s, the
        # 15.9 idle-close drill caught it). Only the CONNECT is bounded.
        # فارسی: فقط مکثِ «اتصال» لایهٔ کانال محدود است؛ خواندن، نظرسنجیِ
        # بلندِ BRPOP است و سقفِ خواندن روی آن، اتصال‌های بی‌کار را می‌کُشد.
        "CONFIG": {"hosts": [{
            "address": REDIS_URL + "/2",
            "socket_connect_timeout": 1,
        }]},
    }
}


