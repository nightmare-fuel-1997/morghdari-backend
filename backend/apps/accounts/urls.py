"""accounts URLs — mounted at api/v1/ (contract v1.4.3 paths, verbatim).

فارسی: مسیرهای احراز هویت؛ دقیقاً مطابق قرارداد OpenAPI.
"""

from django.urls import path

from apps.accounts.views import LoginView, RefreshView

app_name = "accounts"

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/token/refresh/", RefreshView.as_view(), name="auth-refresh"),
]
