"""
API documentation endpoints (owner-requested at v0.1 close).

فارسی: صفحهٔ مستندات API از همین نسخه باید بالا بیاید — اسکیمای OpenAPI معتبر
و دو رابط Swagger/Redoc؛ حتی وقتی هنوز هیچ endpointی وجود ندارد.

With zero business endpoints the schema is intentionally near-empty; the point
is that the plumbing is proven BEFORE v0.2 lands real views, and every future
endpoint self-documents.
"""

import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_openapi_schema_serves_valid_document(client):
    # The schema endpoint negotiates content: default is YAML
    # (application/vnd.oai.openapi), so ask for JSON explicitly.
    res = client.get(reverse("schema"), HTTP_ACCEPT="application/json")
    assert res.status_code == 200
    doc = res.json()
    assert doc["openapi"].startswith("3.")
    assert doc["info"]["title"] == "Morghdari API"
    # v0.1 has no authenticated API surface yet: paths must be an (empty) object,
    # never missing — a missing key would mean spectacular failed silently.
    assert isinstance(doc.get("paths"), dict)


@pytest.mark.django_db
def test_swagger_ui_and_redoc_pages_render(client):
    ui = client.get(reverse("swagger-ui"))
    redoc = client.get(reverse("redoc"))
    assert ui.status_code == 200
    assert redoc.status_code == 200
    # The Swagger shell must reference the schema URL it is meant to render.
    assert "/api/schema/" in ui.content.decode()
