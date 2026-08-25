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

    urlpatterns += [
        re_path(r"^demo/(?P<path>(?:css|js)/.+)$", _static_serve, {"document_root": _DEMO_ROOT}),
        path("demo/", lambda request: _static_serve(request, "index.html", document_root=_DEMO_ROOT)),
    ]
