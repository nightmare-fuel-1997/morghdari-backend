"""URL root — grows version by version.

فارسی: ریشهٔ آدرس‌ها؛ با هر نسخه کامل‌تر می‌شود.
v0.1: admin + DRF API index + OpenAPI schema/Swagger UI/ReDoc. Real endpoints
arrive v0.2+; the OpenAPI contract (backend_docs) is the source of truth for
every path we will add.
"""

from django.contrib import admin
from django.urls import include, path
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
    # API documentation (owner-requested at v0.1 close):
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
