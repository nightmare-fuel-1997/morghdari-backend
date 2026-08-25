"""accounts URLs — mounted at api/v1/ (contract v1.4.3 paths, verbatim).

فارسی: مسیرهای احراز هویت؛ دقیقاً مطابق قرارداد OpenAPI.
"""

from django.urls import path

from apps.accounts.views import (
    LoginView,
    RefreshView,
    deactivate_member,
    org_members,
)

app_name = "accounts"

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/token/refresh/", RefreshView.as_view(), name="auth-refresh"),
    # Contract v1.4.3 org-member management (owner-exclusive writes):
    path("org/members/", org_members, name="org-members"),
    path(
        "org/members/<int:member_id>/deactivate/",
        deactivate_member,
        name="org-member-deactivate",
    ),
]
