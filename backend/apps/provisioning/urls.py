from django.urls import path

from apps.provisioning.api import (
    approve_gateway,
    city_list,
    gateway_config,
    gateway_requests,
    location_tree,
    province_list,
    provisioning_sync,
    reject_gateway,
    resume_gateway,
    review_gateway_request,
    rotate_credential,
    rotation_ack,
    saloon_positions,
    suspend_gateway,
    village_list,
)

app_name = "provisioning"

urlpatterns = [
    # M6-01 geography reads (installer drill-down, contract v1.5.0)
    path("provisioning/location-tree/", location_tree, name="provisioning-location-tree"),
    path("provisioning/provinces/", province_list, name="provisioning-provinces"),
    path("provisioning/cities/", city_list, name="provisioning-cities"),
    path("provisioning/villages/", village_list, name="provisioning-villages"),
    path(
        "provisioning/saloons/<int:saloon_id>/positions/",
        saloon_positions,
        name="provisioning-saloon-positions",
    ),
    path("provisioning/sync/", provisioning_sync, name="provisioning-sync"),
    path("gateway-requests/", gateway_requests, name="gateway-requests"),
    path("gateway-requests/<int:request_id>/review/", review_gateway_request, name="gateway-request-review"),
    # M5-05 farm-manager lifecycle — contract v1.5.0 paths verbatim
    # (approve/reject live under /provisioning/gateways/{gateway_slot_id}/;
    # the diff test caught the old unprefixed mount as contract drift).
    path("provisioning/gateways/<str:gateway_slot_id>/approve/", approve_gateway, name="gateway-approve"),
    path("provisioning/gateways/<str:gateway_slot_id>/reject/", reject_gateway, name="gateway-reject"),
    # v0.5-delivered lifecycle/rotation controls — NOT yet in the yaml
    # (contract-erratum pending; allowlisted in test_openapi_contract.py).
    path("gateways/<str:gateway_slot_id>/suspend/", suspend_gateway, name="gateway-suspend"),
    path("gateways/<str:gateway_slot_id>/resume/", resume_gateway, name="gateway-resume"),
    # M5-06 gateway config fetch (contract path /gateways/{gateway_slot_id}/config/)
    path("gateways/<str:gateway_slot_id>/config/", gateway_config, name="gateway-config"),
    # M5-08 staged credential rotation
    path("gateways/<str:gateway_slot_id>/rotate-credential/", rotate_credential, name="gateway-rotate-credential"),
    path("gateways/<str:gateway_slot_id>/rotation-ack/", rotation_ack, name="gateway-rotation-ack"),
]
