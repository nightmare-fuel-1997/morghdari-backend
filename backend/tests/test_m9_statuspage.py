"""v0.9 M9-04/B4 acceptance — public status page (MET-STATUS).

Unauthenticated JSON + tiny HTML from blackbox probe history ONLY (nothing
org-scoped is ever touched — leak-proof by construction). Prometheus is
mocked at the _prom_query seam (stdlib client); the failure mode (unknown,
never 500) is a first-class assertion.
"""

from unittest import mock

import pytest
from django.test import RequestFactory
from rest_framework.test import force_authenticate


def _prom_result(pairs):
    return {
        "data": {
            "result": [
                {"metric": {"job": job, "instance": target}, "value": [0, str(share)]}
                for job, target, share in pairs
            ]
        }
    }


def _get(accept="application/json", user=None):
    from apps.monitoring.views import status_page

    request = RequestFactory().get("/api/v1/status/", HTTP_ACCEPT=accept)
    if user is not None:
        force_authenticate(request, user=user)
    return status_page(request)


@pytest.mark.django_db
def test_public_json_operational_min_wins():
    with mock.patch(
        "apps.monitoring.views._prom_query",
        return_value=_prom_result([
            ("blackbox-external", "https://api.morghdari.com/api/v1/health/", 0.9999),
            ("blackbox-external-region2", "http://daphne:8000/api/v1/health/", 0.9995),
        ]),
    ):
        response = _get()
    assert response.status_code == 200
    assert response.data["status"] == "operational"
    assert response.data["uptime_14d_pct"] == 99.95  # minimum, fail-closed
    assert len(response.data["probers"]) == 2
    assert response.data["checked_at"]


@pytest.mark.django_db
def test_below_slo_is_degraded():
    with mock.patch(
        "apps.monitoring.views._prom_query",
        return_value=_prom_result([("blackbox-external", "https://x/health/", 0.998)]),
    ):
        response = _get()
    assert response.data["status"] == "degraded"
    assert response.data["uptime_14d_pct"] == 99.8


@pytest.mark.django_db
def test_prometheus_down_is_unknown_never_500():
    with mock.patch("apps.monitoring.views._prom_query", side_effect=TimeoutError):
        response = _get()
    assert response.status_code == 200
    assert response.data["status"] == "unknown"
    assert response.data["uptime_14d_pct"] is None
    assert response.data["probers"] == []


@pytest.mark.django_db
def test_html_variant_renders_status():
    with mock.patch(
        "apps.monitoring.views._prom_query",
        return_value=_prom_result([("blackbox-external", "https://x/health/", 1.0)]),
    ):
        response = _get(accept="text/html")
    assert response.status_code == 200
    assert "text/html" in response["Content-Type"]
    response.render()
    body = response.content.decode()
    assert "operational" in body and "100.0" in body
