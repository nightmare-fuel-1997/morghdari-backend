"""OpenAPI contract diff test — `implemented == contract` (M6-08 gate).

Scope (M6-08, versions/v0.6/TASKS.md): the non-platform, non-§4n paths of the
v1.5.0 yaml (34 paths) PLUS the v1.7.0 ops paths ratified at M6-10 (the
ratification commit enters the yaml; spec docs/api-ops-v1.md).
M7-07 (2026-09-08): the ``/platform/`` exclusion is GONE — all 4 support paths
+ the 2 MFA paths are implemented, so ``implemented == contract`` now covers
them. Remaining exclusions: the 10 §4n paths (v0.9) and ``/api/compat/*``
(M6-09, v1.6.0).

Mechanics:
- ``implemented - documented`` must ALWAYS be empty (no undocumented extras —
  catches trailing-slash drift, method typos, rogue endpoints).
- ``documented - implemented`` must equal ``KNOWN_GAPS`` exactly — a constant
  that may only SHRINK as M6-01..M6-03 land. A stale entry (route implemented
  but still listed) fails the same as a missing route. When it reaches ∅ the
  pair of asserts IS the full equality gate.

pyyaml is NOT pinned in requirements: drf-spectacular hard-depends on it, so
it is present in every environment that can run this suite.
"""

from pathlib import Path

import pytest
import yaml
from django.urls import URLPattern, URLResolver, get_resolver

CONTRACT = Path(__file__).resolve().parent.parent / "openapi.yaml"

# M7-07: was ("/platform/",) "v0.7 scope" — all /platform/support/* paths and
# the MFA pair are implemented now, so nothing is prefix-excluded anymore.
# M7-15: the three §4n report paths left the exclusion set (implemented —
# v0.9 scope pulled forward); /vet-catalog/ and the remaining §4n rows stay
# excluded until their waves.
EXCLUDED_PREFIXES = ()
EXCLUDED_EXACT = {
    # §4n paths — v0.9 scope (Tracker §4n; VERSIONS.md §v0.9)
    "/vet-catalog/",
    "/me/panels/",
    "/panels/{role}/",
    "/panels/users/{user_id}/",
    "/me/alert-subscriptions/",
    "/farms/{farm_id}/events/",
    "/support-tickets/",
}

VERBS = ("get", "post", "put", "patch", "delete")

# v0.5-delivered FM lifecycle/rotation controls that the yaml never documented
# (the support-side suspend/resume IS documented under /platform/...; these are
# the manager-side twins). A contract erratum must ratify them in the docs
# repo; until then they are the ONLY tolerated implemented-but-undocumented
# routes, and this constant must not grow.
ERRATUM_PENDING = {
    ("post", "/gateways/{gateway_slot_id}/suspend/"),
    ("post", "/gateways/{gateway_slot_id}/resume/"),
    ("post", "/gateways/{gateway_slot_id}/rotate-credential/"),
    ("post", "/gateways/{gateway_slot_id}/rotation-ack/"),
}

# The remaining v0.6 build plan. Shrinks to ∅ by M6-08 — a stale entry fails
# exactly like a missing one. M6-01 + M6-02 emptied their entries; M6-03
# empties the last 5 (water/biosecurity/capabilities).
KNOWN_GAPS: set = set()

_API_ROOTS = (
    "apps.accounts.urls",
    "apps.telemetry.urls",
    "apps.provisioning.urls",
    "apps.farms.urls",
    "apps.alerts.urls",
    "apps.monitoring.urls",
    "apps.monitoring.urls_ops",  # M6-10 ops wave (contract v1.7.0)
    "apps.reports.urls",  # M7-15 §4s reports base (v0.9 scope pulled forward)
)


def _documented_full():
    """Every route in the v1.5.0 yaml — used to judge IMPLEMENTED extras (a
    rogue route is one the contract never documents, platform paths included)."""
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    routes = set()
    for path, item in spec["paths"].items():
        for verb in VERBS:
            if verb in item:
                routes.add((verb, path))
    return routes


def _documented():
    """The M6-08 gate scope: the yaml minus platform (v0.7) and §4n paths."""
    full = _documented_full()
    scoped = set()
    for verb, path in full:
        if path.startswith(EXCLUDED_PREFIXES) or path in EXCLUDED_EXACT:
            continue
        scoped.add((verb, path))
    return scoped


def _django_pattern_to_template(route: str) -> str:
    """'saloons/<int:saloon_id>/telemetry/' -> '/saloons/{saloon_id}/telemetry/'."""
    import re

    out = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", route)
    return "/" + out.lstrip("/")


def _view_methods(callback) -> set:
    """HTTP verbs a URL callback serves (DRF CBV, api_view FBV, plain FBV)."""
    methods = set()
    view_cls = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
    source = view_cls if view_cls is not None else callback
    for verb in VERBS:
        func = getattr(source, verb, None)
        if func is not None and not getattr(func, "__isabstractmethod__", False):
            methods.add(verb)
    return methods


def _implemented():
    """Walk the three /api/v1/ app urlconfs → {(verb, '/contract/style/path/')}. """
    routes = set()
    for root in _API_ROOTS:
        resolver = URLResolver([], root)
        def walk(res, prefix=""):
            for pat in res.url_patterns:
                if isinstance(pat, URLResolver):
                    walk(pat, prefix + str(pat.pattern))
                elif isinstance(pat, URLPattern):
                    template = _django_pattern_to_template(prefix + str(pat.pattern))
                    for verb in _view_methods(pat.callback):
                        routes.add((verb, template))
        walk(resolver)
    return routes


@pytest.mark.django_db(transaction=False)
def test_no_undocumented_api_routes():
    extras = _implemented() - _documented_full() - ERRATUM_PENDING
    assert extras == set(), (
        "Routes implemented but NOT in the v1.5.0 contract — contract first "
        "(only the 4 ERRATUM_PENDING v0.5 lifecycle/rotation twins are "
        "tolerated until their erratum ratifies them):\n"
        + "\n".join(sorted(f"  {v.upper():6} {p}" for v, p in extras))
    )


@pytest.mark.django_db(transaction=False)
def test_contract_gaps_shrink_monotonically():
    documented = _documented()
    implemented = _implemented()
    missing = documented - implemented
    stale = missing - KNOWN_GAPS
    assert stale == set(), (
        "Contract routes still unimplemented but MISSING from KNOWN_GAPS "
        "(add them with their task id, never silently):\n"
        + "\n".join(sorted(f"  {v.upper():6} {p}" for v, p in stale))
    )
    done = KNOWN_GAPS - missing
    assert done == set(), (
        "KNOWN_GAPS contains stale entries — routes now implemented: "
        "remove them from the constant:\n"
        + "\n".join(sorted(f"  {v.upper():6} {p}" for v, p in done))
    )


def test_known_gaps_ends_empty():
    """M6-08 exit gate: the diff test IS `implemented == contract` when this
    constant is empty — keep it imported here so the flip is greppable."""
    assert isinstance(KNOWN_GAPS, (set, frozenset))
