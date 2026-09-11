"""URL root — grows version by version.

فارسی: ریشهٔ آدرس‌ها؛ با هر نسخه کامل‌تر می‌شود.
v0.4: admin + DRF API index + OpenAPI schema/Swagger UI/ReDoc. v0.5 adds the
provisioning endpoints (gateway requests/review/sync).
"""

from django.contrib import admin
from django.urls import include, path
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