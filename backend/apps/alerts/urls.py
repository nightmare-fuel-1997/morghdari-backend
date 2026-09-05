"""alerts URLs — mounted at api/v1/ (contract v1.5.0 paths, verbatim).

M6-01: saloon alerts + ack. M6-02 adds the v1.3 KPI/dashboard surface
(weight-samples, feed-intake, saloon kpis) from kpi_views.
"""

from django.urls import path

from apps.alerts.kpi_views import (
    feed_intake,
    me_dashboard_config,
    role_dashboard_config,
    saloon_kpis,
    weight_samples,
)
from apps.alerts.views import alert_acknowledge, saloon_alerts

app_name = "alerts"

urlpatterns = [
    path("saloons/<int:saloon_id>/alerts/", saloon_alerts, name="saloon-alerts"),
    path("alerts/<uuid:alert_id>/acknowledge/", alert_acknowledge, name="alert-acknowledge"),
    # M6-02 — v1.3 KPI + dashboard (contract v1.5.0, verbatim paths)
    path(
        "saloons/<int:saloon_id>/flocks/<int:flock_id>/weight-samples/",
        weight_samples,
        name="weight-samples",
    ),
    path(
        "saloons/<int:saloon_id>/flocks/<int:flock_id>/feed-intake/",
        feed_intake,
        name="feed-intake",
    ),
    path("saloons/<int:saloon_id>/kpis/", saloon_kpis, name="saloon-kpis"),
    path("me/dashboard-config/", me_dashboard_config, name="me-dashboard-config"),
    path(
        "dashboard-config/<str:role>/",
        role_dashboard_config,
        name="role-dashboard-config",
    ),
]
