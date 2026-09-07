"""farms URLs — mounted at api/v1/ (contract v1.5.0 paths, verbatim, M6-01)."""

from django.urls import path

from apps.farms.api import (
    farm_list,
    farm_overview,
    flock_cycles,
    operational_state,
    saloon_detail,
    saloon_list,
)

app_name = "farms"

urlpatterns = [
    path("farms/", farm_list, name="farm-list"),
    path("saloons/", saloon_list, name="saloon-list"),
    path("saloons/<int:saloon_id>/", saloon_detail, name="saloon-detail"),
    path("farms/<int:farm_id>/overview/", farm_overview, name="farm-overview"),
    path(
        "saloons/<int:saloon_id>/operational-state/",
        operational_state,
        name="saloon-operational-state",
    ),
    path("saloons/<int:saloon_id>/flocks/", flock_cycles, name="flock-cycles"),
]
