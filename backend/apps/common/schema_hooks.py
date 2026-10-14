"""drf-spectacular postprocessing hooks (docs-only, zero runtime effect).

drop_legacy_compat_aliases: the legacy /api/... Flutter twins share view
functions with the canonical /api/compat/v1/... routes, so per-view
exclude=True would kill both mounts. Filter the generated schema instead:
keep /api/v1/*, /api/compat/v1/* and the docs pages (/api/schema, /api/docs,
/api/redoc, /api/ws-docs, /api/ws-schema); drop the legacy twins.
The twins stay SERVED (Flutter still calls them) — they are just documented
once, under their canonical prefix, per the locked OpenAPI scope.
"""

_LEGACY_PREFIX = "/api/"
_KEPT_PREFIXES = (
    "/api/v1/",
    "/api/compat/v1/",
    "/api/schema",
    "/api/docs",
    "/api/redoc",
    "/api/ws-docs",
    "/api/ws-schema",
)


def drop_legacy_compat_aliases(result, generator, request, public):
    """POSTPROCESSING_HOOKS entry — removes legacy /api/ alias paths."""
    paths = result.get("paths", {})
    result["paths"] = {
        path: item
        for path, item in paths.items()
        if not path.startswith(_LEGACY_PREFIX)
        or path.startswith(_KEPT_PREFIXES)
    }
    return result


def add_metrics_scrape_path(result, generator, request, public):
    """POSTPROCESSING_HOOKS entry — injects GET /api/v1/metrics/.

    The Prometheus scrape endpoint is a plain Django view (NOT @api_view —
    the exposition format is text/plain, DRF negotiation would 400 it), so
    spectacular's introspector never sees it even though the route is
    served and contract-documented (Monitoring tag, openapi.yaml §3.1).
    Mirror the canonical yaml entry verbatim so the generated docs carry it.
    """
    if "/api/v1/metrics/" in result.get("paths", {}):
        return result
    result["paths"]["/api/v1/metrics/"] = {
        "get": {
            "operationId": "api_v1_metrics_retrieve",
            "tags": ["Monitoring"],
            "summary": "Prometheus scrape endpoint (Observability Runbook §3.1)",
            "description": (
                "Prometheus exposition format (text/plain). Scraped by the "
                "monitoring stack directly on the internal network "
                "(daphne:8000); nginx returns 404 for it publicly. "
                "Worker-side counters/latencies are bridged via Redis and "
                "refreshed at scrape time."
            ),
            "security": [],
            "responses": {
                "200": {
                    "description": "Prometheus exposition format",
                    "content": {
                        "text/plain": {"schema": {"type": "string"}}
                    },
                }
            },
        }
    }
    return result
