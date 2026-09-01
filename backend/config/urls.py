"""URL root — grows version by version.

فارسی: ریشهٔ آدرس‌ها؛ با هر نسخه کامل‌تر می‌شود.
v0.1: admin + DRF API index + OpenAPI schema/Swagger UI/ReDoc. Real endpoints
arrive v0.2+; the OpenAPI contract (backend_docs) is the source of truth for
every path we will add.
"""

import os

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    path("admin/", admin.site.urls),  # system_configuration panel lives here (D21)
    path("api/v1/", include("rest_framework.urls", namespace="rest_framework")),
    # v0.2 auth endpoints (contract v1.4.3: /auth/login/, /auth/token/refresh/)
    path("api/v1/", include("apps.accounts.urls")),
    # v0.3 telemetry read API (contract: /saloons/{id}/telemetry/)
    path("api/v1/", include("apps.telemetry.urls")),
    # v0.5 provisioning request/review flow.
    path("api/v1/", include("apps.provisioning.urls")),
    # API documentation (owner-requested at v0.1 close):
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

# ---------------------------------------------------------------------------
# Demo console (standalone showcase of v0.2 capabilities) — GATED route.
# Same-origin on purpose: browser and API share :8000, so the demo needs no
# CORS surface and ships zero JS dependencies. It is exposed ONLY when DEBUG
# is on or MORGHDARI_DEMO_UI=1 is set; the production image never serves it.
# فارسی: کنسول دمو فقط در حالت دیباگ یا با متغیر محیطی مخصوص سرو می‌شود.
# ---------------------------------------------------------------------------
if settings.DEBUG or os.environ.get("MORGHDARI_DEMO_UI") == "1":
    from django.views.static import serve as _static_serve

    # NOTE: settings.BASE_DIR points at backend/config (historical quirk), so
    # the repo-root demo-ui folder is TWO parents up, not one.
    _DEMO_ROOT = str(settings.BASE_DIR.parent.parent / "demo-ui")

    from apps.alerts import demo_views as _alert_demo_views
    from apps.telemetry import demo_views

    _DEMO_DIST = str(settings.BASE_DIR.parent.parent / "demo-ui" / "dist")

    urlpatterns += [
        # React app (Vite build → demo-ui/dist/)
        re_path(r"^demo/(?P<path>assets/.+)$", _static_serve, {"document_root": _DEMO_DIST}),
        path("demo/", lambda request: _static_serve(request, "index.html", document_root=_DEMO_DIST)),
        # Legacy engineering console + dashboard (demo-ui root)
        re_path(r"^demo/(?P<path>(?:css|js)/.+)$", _static_serve, {"document_root": _DEMO_ROOT}),
        path("demo/console/", lambda request: _static_serve(request, "index.html", document_root=_DEMO_ROOT)),
        path("demo/dashboard/", lambda request: _static_serve(request, "dashboard.html", document_root=_DEMO_ROOT)),
        # public barn identity for the login backdrop (no JWT — counts only)
        path("demo/api/identity/", demo_views.demo_identity, name="demo-identity"),
        # live pipeline introspection (JWT-required; demo data only)
        path("demo/api/pipeline/", demo_views.pipeline_status, name="demo-pipeline"),
        path("demo/api/dlq/", demo_views.dlq_tail, name="demo-dlq"),
        path("demo/api/simulate/", demo_views.simulate_reading, name="demo-simulate"),
        path("demo/api/farm/", demo_views.farm_topology, name="demo-farm"),
        path("demo/api/sensor-off/", demo_views.sensor_off, name="demo-sensor-off"),
        path("demo/api/ack/", demo_views.acknowledge_alert_view, name="demo-ack"),
        # v0.4 alert-engine introspection (JWT-required; read-only)
        # v0.4 demo dashboard: per-saloon sensor inventory + stats, EMQX status
        path("demo/api/saloons/<int:saloon_id>/sensors/", demo_views.saloon_sensors, name="demo-saloon-sensors"),
        path("demo/api/saloons/<int:saloon_id>/stats/", demo_views.saloon_stats, name="demo-saloon-stats"),
        path("demo/api/emqx-status/", demo_views.emqx_status, name="demo-emqx-status"),
        path("demo/api/alerts/quality/", _alert_demo_views.alert_quality, name="demo-alert-quality"),
        path("demo/api/alerts/overview/", _alert_demo_views.alert_overview, name="demo-alert-overview"),
        path("demo/api/alerts/state-machines/", _alert_demo_views.alert_state_machines, name="demo-alert-state"),
    ]
