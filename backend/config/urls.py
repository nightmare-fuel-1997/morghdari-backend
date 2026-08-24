"""URL root — grows version by version.

فارسی: ریشهٔ آدرس‌ها؛ با هر نسخه کامل‌تر می‌شود.
v0.1: admin + DRF API index only. Real endpoints arrive v0.2+; the OpenAPI
contract (backend_docs) is the source of truth for every path we will add.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),  # system_configuration panel lives here (D21)
    path("api/v1/", include("rest_framework.urls", namespace="rest_framework")),
]
