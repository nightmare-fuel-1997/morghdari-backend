"""URL root — grows version by version.

فارسی: ریشهٔ آدرس‌ها؛ با هر نسخه کامل‌تر می‌شود.
v0.4: admin + DRF API index + OpenAPI schema/Swagger UI/ReDoc + gated demo
console. v0.5 adds the provisioning endpoints (gateway requests/review/sync).
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
    path("admin/", admin.site.urls),
    path("api/v1/", include("rest_framework.urls", namespace="rest_framework")),
    path("api/v1/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.telemetry.urls")),
    path("api/v1/", include("apps.provisioning.urls")),
    path("api/v1/", include("apps.farms.urls")),
    path("api/v1/", include("apps.alerts.urls")),
    path("api/v1/", include("apps.monitoring.urls")),
    path("api/v1/", include("apps.reports.urls")),
    # M6-10 ops wave (contract v1.7.0-draft): audit/thresholds/photos/
    # gateways/sessions. Mounted from the SAME app's second urlconf —
    # deliberately NOT in apps.monitoring.urls so the implemented ==
    # contract gate (M6-08) keeps its pre-ratification scope.
    path("api/v1/", include("apps.monitoring.urls_ops")),
    # M6-09 compat layer (v1.6.0-draft): additive prefix + legacy Flutter
    # aliases, same views. NOT in apps.* urlconfs → the implemented ==
    # contract gate's _API_ROOTS walk never sees it (spec §1.2).
    path("api/compat/v1/", include("apps.compat.urls_compat")),
    path("api/", include("apps.compat.urls_compat")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

# ---------------------------------------------------------------------------
# Demo console (live dashboard + pipeline introspection) — GATED routes.
# Same-origin on purpose: browser and API share :8000, so the demo needs no
# CORS surface and ships zero JS dependencies. Exposed ONLY when DEBUG is on
# or MORGHDARI_DEMO_UI=1 is set; the production image never serves it.
# NOTE: settings.BASE_DIR points at backend/config (historical quirk), so the
# repo-root demo-ui folder is TWO parents up, not one.
# فارسی: کنسول دمو فقط در حالت دیباگ یا با متغیر محیطی مخصوص سرو می‌شود.
# ---------------------------------------------------------------------------
if settings.DEBUG or os.environ.get("MORGHDARI_DEMO_UI") == "1":
    from django.views.static import serve as _static_serve

    from apps.telemetry import demo_views

    _DEMO_ROOT = str(settings.BASE_DIR.parent.parent / "demo-ui")
    urlpatterns += [
        re_path(r"^demo/(?P<path>(?:css|js)/.+)$", _static_serve, {"document_root": _DEMO_ROOT}),
        path("demo/", lambda request: _static_serve(request, "index.html", document_root=_DEMO_ROOT)),
        path("demo/api/identity/", demo_views.demo_identity, name="demo-identity"),
        path("demo/api/pipeline/", demo_views.pipeline_status, name="demo-pipeline"),
        path("demo/api/dlq/", demo_views.dlq_tail, name="demo-dlq"),
        path("demo/api/simulate/", demo_views.simulate_reading, name="demo-simulate"),
        path("demo/api/farm/", demo_views.farm_topology, name="demo-farm"),
        path("demo/api/saloons/<int:saloon_id>/sensors/", demo_views.saloon_sensors, name="demo-saloon-sensors"),
        path("demo/api/saloons/<int:saloon_id>/stats/", demo_views.saloon_stats, name="demo-saloon-stats"),
        path("demo/api/emqx-status/", demo_views.emqx_status, name="demo-emqx-status"),
    ]