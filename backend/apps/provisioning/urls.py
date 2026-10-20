from django.urls import path

from apps.provisioning.api import (
    approve_gateway,
    city_list,
    gateway_config,
    install_assignable,
    install_gateway_photo,
    install_job_assign,
    install_job_bind,
    install_job_bind_override,
    install_job_detail,
    install_job_session,
    install_job_sensor_map,
    install_sensor_photo,
    install_job_submit,
    install_jobs,
    provisioning_config_state,
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
    # Phase 2 slice (b) — installation jobs: queue, detail, assign, session.
    # Org-less by design: the installer has no org claim until `session/` mints
    # one (AD-14). Discovered via the admin alias, then read per tenant context.
    path("provisioning/installations/", install_jobs, name="install-jobs"),
    path(
        "provisioning/installations/assignable/",
        install_assignable,
        name="install-assignable",
    ),
    path(
        "provisioning/installations/<int:job_id>/",
        install_job_detail,
        name="install-job-detail",
    ),
    path(
        "provisioning/installations/<int:job_id>/assign/",
        install_job_assign,
        name="install-job-assign",
    ),
    path(
        "provisioning/installations/<int:job_id>/gateway/",
        install_job_bind,
        name="install-job-bind",
    ),
    path(
        "provisioning/installations/<int:job_id>/gateway/override/",
        install_job_bind_override,
        name="install-job-bind-override",
    ),
    path(
        "provisioning/installations/<int:job_id>/session/",
        install_job_session,
        name="install-job-session",
    ),

    # M6-01 geography reads (installer drill-down, contract v1.5.0)
    path(
        "provisioning/installations/<int:job_id>/sensors/",
        install_job_sensor_map,
        name="install-job-sensor-map",
    ),
    path(
        "provisioning/installations/<int:job_id>/gateway/photo/",
        install_gateway_photo,
        name="install-gateway-photo",
    ),
    path(
        "provisioning/installations/<int:job_id>/sensors/<str:slot_id>/photo/",
        install_sensor_photo,
        name="install-sensor-photo",
    ),
    path(
        "provisioning/installations/<int:job_id>/submit/",
        install_job_submit,
        name="install-job-submit",
    ),
    path(
        "provisioning/config-state/",
        provisioning_config_state,
        name="provisioning-config-state",
    ),
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
