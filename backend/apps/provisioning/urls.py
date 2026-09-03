from django.urls import path

from apps.provisioning.api import (
    approve_gateway,
    gateway_config,
    gateway_requests,
    provisioning_sync,
    reject_gateway,
    resume_gateway,
    review_gateway_request,
    rotate_credential,
    rotation_ack,
    suspend_gateway,
)

app_name = "provisioning"

urlpatterns = [
    path("provisioning/sync/", provisioning_sync, name="provisioning-sync"),
    path("gateway-requests/", gateway_requests, name="gateway-requests"),
    path("gateway-requests/<int:request_id>/review/", review_gateway_request, name="gateway-request-review"),
    # M5-05 farm-manager lifecycle
    path("gateways/<str:slot_id>/approve/", approve_gateway, name="gateway-approve"),
    path("gateways/<str:slot_id>/reject/", reject_gateway, name="gateway-reject"),
    path("gateways/<str:slot_id>/suspend/", suspend_gateway, name="gateway-suspend"),
    path("gateways/<str:slot_id>/resume/", resume_gateway, name="gateway-resume"),
    # M5-06 gateway config fetch
    path("gateways/<str:slot_id>/config/", gateway_config, name="gateway-config"),
    # M5-08 staged credential rotation
    path("gateways/<str:slot_id>/rotate-credential/", rotate_credential, name="gateway-rotate-credential"),
    path("gateways/<str:slot_id>/rotation-ack/", rotation_ack, name="gateway-rotation-ack"),
]