"""telemetry URLs — mounted at api/v1/ (contract v1.5.0 paths, verbatim)."""

from django.urls import path

from apps.telemetry.views import (
    farm_telemetry,
    metric_types,
    saloon_analytics_hourly,
    saloon_telemetry,
)

app_name = "telemetry"

urlpatterns = [
    path("saloons/<int:saloon_id>/telemetry/", saloon_telemetry, name="saloon-telemetry"),
    path("farms/<int:farm_id>/telemetry/", farm_telemetry, name="farm-telemetry"),
    path(
        "saloons/<int:saloon_id>/analytics/hourly/",
        saloon_analytics_hourly,
        name="saloon-analytics-hourly",
    ),
    path("metric-types/", metric_types, name="metric-types"),
]
