"""alerts URLs — mounted at api/v1/ (contract v1.5.0 paths, verbatim, M6-01)."""

from django.urls import path

from apps.alerts.views import alert_acknowledge, saloon_alerts

app_name = "alerts"

urlpatterns = [
    path("saloons/<int:saloon_id>/alerts/", saloon_alerts, name="saloon-alerts"),
    path("alerts/<uuid:alert_id>/acknowledge/", alert_acknowledge, name="alert-acknowledge"),
]
