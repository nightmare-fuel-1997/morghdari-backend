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

from apps.telemetry.ws_docs_views import WsDocsView, WsSchemaView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("rest_framework.urls", namespace="rest_framework")),
    path("api/v1/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.telemetry.urls")),
    path("api/v1/", include("apps.provisioning.urls")),
    path("api/v1/", include("apps.farms.urls")),
    path("api/v1/", include("apps.alerts.urls")),
    # v1.7.2 follow-through wave (F1–F15) — new paths only, so mounting these
    # cannot change any pre-existing route. Two of the wave's routes are NOT in
    # these urlconfs on purpose: POST /farms/ and PATCH|DELETE /saloons/{id}/
    # EXTEND the existing views above (api.farm_list / api.saloon_detail),
    # because Django matches the first pattern regardless of method — a second
    # pattern for a shared path would answer 405.
    path("api/v1/", include("apps.farms.urls_follow")),
    path("api/v1/", include("apps.alerts.urls_follow")),
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
    path("api/ws-docs/", WsDocsView.as_view(), name="ws-docs"),
    path("api/ws-schema/", WsSchemaView.as_view(), name="ws-schema"),
]