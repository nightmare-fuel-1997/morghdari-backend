"""telemetry URLs — mounted at api/v1/ (contract v1.4.4 paths, verbatim)."""

from django.urls import path

from apps.telemetry.views import saloon_telemetry

app_name = "telemetry"

urlpatterns = [
    path("saloons/<int:saloon_id>/telemetry/", saloon_telemetry, name="saloon-telemetry"),
]
