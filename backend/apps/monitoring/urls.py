"""monitoring URLs — mounted at api/v1/ (contract v1.5.0 paths, verbatim, M6-01)."""

from django.urls import path

from apps.monitoring.views import hardware_time, health, metrics, status_page

app_name = "monitoring"

urlpatterns = [
    path("health/", health, name="health"),
    path("hardware/time/", hardware_time, name="hardware-time"),
    # v0.9/B4 MET-STATUS: public uptime page (probe aggregates only — no
    # tenant data can leak by construction). nginx must ALLOW it publicly
    # (unlike /metrics/, which stays scrape-only).
    path("status/", status_page, name="status-page"),
    # M7-04: Prometheus scrape endpoint (Observability Runbook §3.1). Plain
    # text exposition — see views.metrics docstring for why it is not a DRF
    # @api_view. nginx 404s it publicly; the scrape targets daphne directly.
    path("metrics/", metrics, name="metrics"),
]
