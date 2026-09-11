"""Generated-schema gate — docs must build clean, and stay in sync with the code.

Why this exists (openapi-annotations branch, 2026-09-11): `backend/openapi.yaml`
is the hand-written CONTRACT, and the drf-spectacular document at
`/api/schema/` (mirrored to `backend/openapi.generated.yaml`) is how the
implemented API documents itself. Before the annotation pass that mirror carried
315 errors across 64 views — every one of them a view whose request/response
shape was INVENTED by spectacular ("unable to guess serializer" fallback) or
omitted entirely. That is worse than no docs: a wrong shape is a bug generator
for client authors.

This gate keeps the mirror honest the same way the contract gate keeps the
routes honest:

1. generation emits **zero warnings and zero errors** — no view may fall back
   to a guessed serializer, and no operationId may collide;
2. the emitted document **validates** as OpenAPI 3.0 (drf-spectacular's own
   validator, the same "you generated garbage" net the CLI exposes);
3. only the canonical prefixes are documented — the legacy `/api/...` Flutter
   twins share view functions with `/api/compat/v1/...`, so they are dropped by
   `apps.common.schema_hooks` instead of being documented twice;
4. every operation carries a human `summary` — the docs page is for integrators,
   not for a path listing.

The committed mirror is compared by ROUTE SET (path → verbs) rather than by
byte/dict equality: drf-spectacular is pinned as `>=0.27`, so description and
formatting churn between versions must not fail the gate, while a forgotten
regeneration after adding/moving an endpoint must.

Drift is checked only when `openapi.generated.yaml` is present, so the suite
stays green on a checkout where the mirror was not committed.
"""

from pathlib import Path

import pytest
import yaml
from drf_spectacular.drainage import reset_generator_stats
from drf_spectacular.validation import validate_schema

MIRROR = Path(__file__).resolve().parent.parent / "openapi.generated.yaml"

# Prefixes the mirror is allowed to document:
#   /api/v1/         — the canonical surface (TUTORIAL)
#   /api/compat/v1/  — the W1–W5 Flutter report aliases (docs/api-compat-flutter-v1.md)
#   /api/schema, /api/docs, /api/redoc — the docs endpoints themselves
KEPT_PREFIXES = ("/api/v1/", "/api/compat/v1/", "/api/schema", "/api/docs", "/api/redoc")

# Ops/notes endpoints that are served but deliberately undocumented (DEBUG-gated
# demo console) are @extend_schema(exclude=True), so they never reach here.
_HIDDEN_PREFIXES = ("/demo/",)


def _generate():
    """(schema, errors, warnings) for the current urlconf — no DB access needed."""
    from drf_spectacular.generators import SchemaGenerator

    reset_generator_stats()
    generator = SchemaGenerator()
    schema = generator.get_schema(request=None, public=True)

    # GeneratorStats is the same collector the CLI's `--fail-on-warn` reads;
    # the private caches are the only way to report WHICH view drifted, so they
    # are read defensively (a future spectacular may rename them).
    from drf_spectacular.drainage import GENERATOR_STATS

    errors = dict(getattr(GENERATOR_STATS, "_error_cache", {}) or {})
    warnings = dict(getattr(GENERATOR_STATS, "_warn_cache", {}) or {})
    return schema, errors, warnings


def test_schema_generates_without_errors_or_warnings():
    """No view may fall back to a guessed serializer (the 315-error baseline)."""
    schema, errors, warnings = _generate()

    assert schema is not None, "schema generation produced no document at all"
    assert not errors, (
        f"{sum(errors.values())} schema error(s) — every view needs an explicit "
        "@extend_schema request/response. First offenders:\n  "
        + "\n  ".join(sorted(errors)[:5])
    )
    assert not warnings, (
        f"{sum(warnings.values())} schema warning(s). First offenders:\n  "
        + "\n  ".join(sorted(warnings)[:5])
    )


def test_generated_schema_is_valid_openapi():
    """The emitted document must satisfy the OpenAPI 3.0 schema itself."""
    schema, _, _ = _generate()
    try:
        validate_schema(schema)
    except Exception as exc:  # pragma: no cover - only trips on a real defect
        pytest.fail(f"generated schema is not valid OpenAPI: {exc}")


def test_only_canonical_prefixes_are_documented():
    """Legacy /api/ twins stay served but documented once (schema_hooks)."""
    schema, _, _ = _generate()
    paths = list((schema or {}).get("paths", {}))
    assert paths, "no paths in the generated schema"

    stray = [
        path
        for path in paths
        if not path.startswith(KEPT_PREFIXES) and not path.startswith(_HIDDEN_PREFIXES)
    ]
    assert not stray, (
        "the legacy `/api/...` compat twins must be dropped by "
        f"apps.common.schema_hooks.drop_legacy_compat_aliases, found: {stray}"
    )


def test_every_operation_has_a_summary():
    """Integrators read summaries; a bare path listing is not documentation."""
    schema, _, _ = _generate()
    missing = [
        f"{method.upper()} {path}"
        for path, item in (schema or {}).get("paths", {}).items()
        for method in ("get", "post", "put", "patch", "delete")
        if method in item and not item[method].get("summary")
    ]
    assert not missing, "operations without a summary:\n  " + "\n  ".join(missing)


def test_servers_entry_is_same_origin():
    """2026-09-11 regression: a hardcoded https://api.morghdari.com first
    server made the Swagger UI Execute against a host that only resolves in
    production — the browser failed with TypeError: NetworkError and the
    response panel read "(Undocumented)". The server must stay a bare
    relative "/", so "Try it out" resolves against the host serving the
    docs on every door (:8000 dev, :443 rehearsal, the VPS)."""
    schema, _, _ = _generate()
    assert (schema or {}).get("servers") == [
        {
            "url": "/",
            "description": "This server — requests resolve against the host serving these docs",
        }
    ], "SERVERS must stay same-origin relative (see SPECTACULAR_SETTINGS comment)"


def _route_set(paths: dict) -> dict:
    """{path: {verbs}} — the version-robust half of the generated mirror."""
    verbs = ("get", "post", "put", "patch", "delete")
    return {
        path: frozenset(method for method in verbs if method in item)
        for path, item in paths.items()
    }


def test_committed_mirror_covers_the_same_routes():
    """`openapi.generated.yaml` must be regenerated when the route set changes.

    Regenerate with:

        python backend/manage.py spectacular --file backend/openapi.generated.yaml
    """
    if not MIRROR.exists():  # pragma: no cover - mirror not committed yet
        pytest.skip("openapi.generated.yaml is not present in this checkout")

    schema, _, _ = _generate()
    live = _route_set(schema.get("paths", {}))
    committed = _route_set(yaml.safe_load(MIRROR.read_text(encoding="utf-8"))["paths"])

    added = {path: verbs for path, verbs in live.items() if committed.get(path) != verbs}
    removed = {path: verbs for path, verbs in committed.items() if path not in live}
    assert not added and not removed, (
        "openapi.generated.yaml is stale — regenerate it.\n"
        f"  routes changed in code: {sorted(added)}\n"
        f"  routes gone from code:  {sorted(removed)}"
    )
