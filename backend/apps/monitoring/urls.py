"""monitoring URLs — mounted at api/v1/ (contract v1.5.0 paths, verbatim, M6-01)."""

from django.urls import path

from apps.monitoring.views import hardware_time, health

app_name = "monitoring"

urlpatterns = [
    path("health/", health, name="health"),
    path("hardware/time/", hardware_time, name="hardware-time"),
]
