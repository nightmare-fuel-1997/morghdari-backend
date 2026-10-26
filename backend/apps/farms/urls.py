"""farms URLs — mounted at api/v1/ (contract v1.5.0 paths, verbatim, M6-01)."""

from django.urls import path

from apps.farms.api import (
    farm_list,
    saloon_sensor_slot_create,
    farm_overview,
    flock_cycles,
    operational_state,
    saloon_detail,
    saloon_list,
)
from apps.farms.api_follow import saloon_sensor_slot_detail

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
    path(
        "saloons/<int:saloon_id>/sensor-slots/",
        saloon_sensor_slot_create,
        name="saloon-sensor-slots",
    ),
    # 2026-10-26: the slot's own write surface — PATCH (retire / re-arm /
    # re-scope) and DELETE (soft-off). Before this, `is_active` had no writer.
    path(
        "saloons/<int:saloon_id>/sensor-slots/<str:slot_id>/",
        saloon_sensor_slot_detail,
        name="saloon-sensor-slot-detail",
    ),
]
