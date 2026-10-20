"""accounts URLs — mounted at api/v1/ (contract v1.4.3 paths, verbatim).

فارسی: مسیرهای احراز هویت؛ دقیقاً مطابق قرارداد OpenAPI.
"""

from django.urls import path

from apps.accounts.views import (
    LoginView,
    RefreshView,
    deactivate_member,
    mfa_confirm,
    mfa_setup,
    org_members,
    support_alert_force_resolve,
    support_gateway_provisioning,
    support_session,
    support_task_dispatch,
    update_member_role,
)
from apps.devices.api_units import (
    unit_claim_code,
    unit_collection,
    unit_credential,
    unit_retire,
)
from apps.accounts.api_hardware import (
    hw_actuator_register,
    hw_actuator_retire,
    hw_gateway_acl_backfill,
    hw_gateway_register,
    hw_gateway_replace,
    hw_gateway_retire,
    hw_sensor_register,
    hw_sensor_retire,
)

app_name = "accounts"

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/token/refresh/", RefreshView.as_view(), name="auth-refresh"),
    # Contract v1.4.3 org-member management (owner-exclusive writes):
    path("org/members/", org_members, name="org-members"),
    path(
        "org/members/<int:member_id>/deactivate/",
        deactivate_member,
        name="org-member-deactivate",
    ),
    # v1.7.2 (F37): owner-exclusive role change. A distinct path from
    # /deactivate/ — both patterns are anchored full matches, so neither can
    # shadow the other.
    path("org/members/<int:member_id>/", update_member_role, name="org-member-role"),
    # Contract v1.4.4: audited support-session opening (staff-not-superuser).
    path("platform/support/session/", support_session, name="support-session"),
    # M7-07: staff MFA enrollment + the 3 whitelisted support ops (v1.2). The
    # /platform/support/* paths ARE contract paths (v1.2) — the M6-08 gate's
    # /platform/ exclusion was "until M7-07 implements them".
    path("auth/mfa/setup/", mfa_setup, name="mfa-setup"),
    path("auth/mfa/confirm/", mfa_confirm, name="mfa-confirm"),
    path(
        "platform/support/gateways/<str:gateway_slot_id>/provisioning/",
        support_gateway_provisioning,
        name="support-gateway-provisioning",
    ),
    path(
        "platform/support/alerts/<uuid:alert_id>/force-resolve/",
        support_alert_force_resolve,
        name="support-alert-force-resolve",
    ),
    path("platform/support/tasks/drain/", support_task_dispatch, name="support-task-dispatch"),
    # Phase 1 (2026-10-18): ORG-LESS factory inventory. Distinct from
    # /platform/support/* on purpose — those require an org-scoped support
    # session, and the Hardware Team works before any org exists (AD-1/AD-13).
    path("platform/devices/units/", unit_collection, name="platform-units"),
    path(
        "platform/devices/units/<str:unit_id>/credential/",
        unit_credential,
        name="platform-unit-credential",
    ),
    path(
        "platform/devices/units/<str:unit_id>/claim-code/",
        unit_claim_code,
        name="platform-unit-claim-code",
    ),
    path(
        "platform/devices/units/<str:unit_id>/retire/",
        unit_retire,
        name="platform-unit-retire",
    ),
    # v0.9 H0-H2: Hardware Team write path — register + retire x gateway /
    # sensor / actuator, behind the same audited support session as M7-07 ops.
    path("platform/support/hardware/gateways/", hw_gateway_register, name="hw-gateway-register"),
    path(
        "platform/support/hardware/gateways/<int:hw_id>/retire/",
        hw_gateway_retire,
        name="hw-gateway-retire",
    ),
    # v0.9 H3/H4 — Django resolves the FIRST matching pattern regardless of
    # method (v1.7.2 lesson): these full paths cannot shadow each other
    # (<int:hw_id>/replace/ vs <str:slot_id>/acl-backfill/ are disjoint).
    path(
        "platform/support/hardware/gateways/<int:hw_id>/replace/",
        hw_gateway_replace,
        name="hw-gateway-replace",
    ),
    path(
        "platform/support/hardware/gateways/<str:slot_id>/acl-backfill/",
        hw_gateway_acl_backfill,
        name="hw-gateway-acl-backfill",
    ),
    path("platform/support/hardware/sensors/", hw_sensor_register, name="hw-sensor-register"),
    path(
        "platform/support/hardware/sensors/<int:hw_id>/retire/",
        hw_sensor_retire,
        name="hw-sensor-retire",
    ),
    path("platform/support/hardware/actuators/", hw_actuator_register, name="hw-actuator-register"),
    path(
        "platform/support/hardware/actuators/<int:hw_id>/retire/",
        hw_actuator_retire,
        name="hw-actuator-retire",
    ),
]
