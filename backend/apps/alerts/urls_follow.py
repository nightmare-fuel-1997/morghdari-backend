"""Alerts follow-through routes — contract v1.7.2 (spec §1, F9/F11/F12/F13)
+ v0.9/B2 subscriptions (GS-ROUTING).

Mounted at api/v1/ from config/urls.py and listed in the contract gate's
`_API_ROOTS`. Every route is a NEW path, so mounting it cannot change any
existing behaviour:

    GET    /alerts/                    engineer+   org-wide feed (F9)
    GET    /maintenance-mutes/         manager+    first read path (F11)
    POST   /maintenance-mutes/         manager+    first write path (F12)
    DELETE /maintenance-mutes/{id}/    manager+    revoke early (F13)

`/alerts/` and the long-standing `/alerts/{alert_id}/acknowledge/` coexist
because both patterns are anchored full matches — the single-segment route can
never swallow the two-segment one.

فارسی: فهرستِ سازمانیِ هشدارها و پنجره‌های بی‌صداکننده — همه آدرس‌ها تازه‌اند و
هیچ رفتاری را تغییر نمی‌دهند.
"""

from django.urls import path

from apps.alerts.api_follow import (
    alert_feed,
    alert_subscription_unmute,
    alert_subscriptions,
    maintenance_mute_revoke,
    maintenance_mutes,
)
from apps.alerts.api_panels import (
    panels_me,
    role_panel_permission_delete,
    role_panel_permissions,
    user_panel_override_delete,
    user_panel_overrides,
)

app_name = "alerts-follow"

urlpatterns = [
    path("alerts/", alert_feed, name="alert-feed"),
    path("maintenance-mutes/", maintenance_mutes, name="maintenance-mutes"),
    path(
        "maintenance-mutes/<int:mute_id>/",
        maintenance_mute_revoke,
        name="maintenance-mute-revoke",
    ),
    # GET+POST share ONE view (Django resolves the first matching pattern
    # regardless of method — v1.7.2 lesson, same as maintenance_mutes).
    path("alert-subscriptions/", alert_subscriptions, name="alert-subscriptions"),
    path(
        "alert-subscriptions/<str:metric_key>/",
        alert_subscription_unmute,
        name="alert-subscription-unmute",
    ),
    # v0.9/B3 granular panels (D24-2). Same GET+POST twin rule as above.
    path("panels/me/", panels_me, name="panels-me"),
    path("panels/role-permissions/", role_panel_permissions, name="role-panel-permissions"),
    path(
        "panels/role-permissions/<int:perm_id>/",
        role_panel_permission_delete,
        name="role-panel-permission-delete",
    ),
    path("panels/user-overrides/", user_panel_overrides, name="user-panel-overrides"),
    path(
        "panels/user-overrides/<int:override_id>/",
        user_panel_override_delete,
        name="user-panel-override-delete",
    ),
]
