"""Ops URLs — M6-10 (contract v1.7.0 paths, verbatim from spec §2).

Mounted at api/v1/ in config/urls.py. Every path below is NEW (checked
against alerts/provisioning/farms/telemetry/accounts urlconfs — no
collisions; the /gateways/... subpaths O7–O9 add are distinct from the
v0.5 provisioning actions like /gateways/{id}/suspend/).
"""

from django.urls import path

from apps.monitoring import ops_views

app_name = "ops"

urlpatterns = [
    # O1 — audit log query
    path("audit-logs/", ops_views.audit_logs, name="audit-logs"),
    # O2/O3 — thresholds (read engineer+, write manager+)
    path("thresholds/", ops_views.thresholds, name="thresholds"),
    path("thresholds/<int:rule_id>/", ops_views.threshold_update, name="threshold-update"),
    # O4/O13 — template catalog (engineer+) + draft authoring (engineer+,
    # farm-scoped). ONE pattern, both methods (the O5/O6 pattern: a second
    # pattern for the same route would shadow the POST and answer 405).
    path("threshold-templates/", ops_views.threshold_templates, name="threshold-templates"),
    # O14 — edit a draft (409 on a published template)
    path(
        "threshold-templates/<int:template_id>/",
        ops_views.threshold_template_update,
        name="threshold-template-update",
    ),
    # O15 — publish a draft (owner/manager; re-caches affected saloons)
    path(
        "threshold-templates/<int:template_id>/publish/",
        ops_views.threshold_template_publish,
        name="threshold-template-publish",
    ),
    # O16 — saloon → template assignment (GET any member, PUT owner/manager)
    path(
        "saloons/<int:saloon_id>/threshold-template/",
        ops_views.saloon_threshold_template,
        name="saloon-threshold-template",
    ),
    # O5/O6 — saloon photos (ONE path, both methods; a duplicate pattern
    # would shadow the POST and answer 405)
    path(
        "saloons/<int:saloon_id>/photos/",
        ops_views.saloon_photos,  # api_view(["GET", "POST"]) — the upload view
        name="saloon-photos",
    ),
    # O7/O8/O9 — gateway reads (state changes stay in /provisioning/... +
    # the v0.5 manager actions)
    path("gateways/", ops_views.gateways, name="gateways"),
    path("gateways/<str:slot>/", ops_views.gateway_detail, name="gateway-detail"),
    path("gateways/<str:slot>/diagnostics/", ops_views.gateway_diagnostics, name="gateway-diagnostics"),
    # O10 — password change (auth_strict 3/min)
    path("auth/password/change/", ops_views.password_change, name="password-change"),
    # O11/O12 — session register
    path("auth/sessions/", ops_views.sessions, name="sessions"),
    path("auth/sessions/<int:session_id>/", ops_views.session_revoke, name="session-revoke"),
]
