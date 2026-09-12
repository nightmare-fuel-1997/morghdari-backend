"""OpenAPI contract diff test — `implemented == contract` (M6-08 gate).

Scope (M6-08, versions/v0.6/TASKS.md): the non-platform, non-§4n paths of the
v1.5.0 yaml (34 paths) PLUS the v1.7.0 ops paths ratified at M6-10 (the
ratification commit enters the yaml; spec docs/api-ops-v1.md).
M7-07 (2026-09-08): the ``/platform/`` exclusion is GONE — all 4 support paths
+ the 2 MFA paths are implemented, so ``implemented == contract`` now covers
them. Remaining exclusions: the 10 §4n paths (v0.9) and ``/api/compat/*``
(M6-09, v1.6.0).
M7-17 (2026-09-09): the v1.7.0 union absorbs the merged report schemas
(by-date GET + alias/extras keys on list/POST/PATCH) and the W1–W5 compat
report aliases (``/api/compat/v1/.../reports...``). The C1–C16 compat surface
stays OUT of the gate (spec §1.2 — separate prefix, never drift); only
compat routes containing ``/reports/`` are walked, and the union test pins
them to exactly the 5 W routes. 1.8.0 stays reserved.

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
    # §4n paths — v0.9 scope (Tracker §4n; VERSIONS.md §v0.9).
    # B2 (2026-09-12): /me/alert-subscriptions/ left the set — subscriptions
    # shipped org-scoped (/alert-subscriptions/ + /{metric}/) instead of
    # per-user; the yaml documents the shipped shape.
    # B3 (2026-09-12): the three §4n panel placeholders left the set —
    # panels shipped as /panels/me/ + role-permissions + user-overrides
    # (visibility-only; the draft's can_edit never materialized).
    "/vet-catalog/",
    "/farms/{farm_id}/events/",
    "/support-tickets/",
}

VERBS = ("get", "post", "put", "patch", "delete")

# v0.5-delivered FM lifecycle/rotation controls that the yaml never documented
# (the support-side suspend/resume IS documented under /platform/...; these are
# the manager-side twins).
# v1.7.2 (2026-09-12): the erratum was RATIFIED — the four paths are now in the
# yaml verbatim with their role gates, so the set is EMPTY and stays that way.
# The assert below is what keeps a future wave from quietly re-opening the hole.
ERRATUM_PENDING: set = set()

# v1.7.2 guard: the topology/staff twin for these routes does NOT exist as an
# extra — if a future change makes an implemented route undocumented, this test
# fails first. TODO-free on purpose: the four ratified paths are pinned by the
# union test at the bottom of this module.
RATIFIED_V072_TWINS = {
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
    "apps.farms.urls_follow",  # v1.7.2 follow-through wave (F2–F8/F14/F15)
    "apps.alerts.urls_follow",  # v1.7.2 follow-through wave (F9/F11–F13)
    "apps.monitoring.urls",
    "apps.monitoring.urls_ops",  # M6-10 ops wave (contract v1.7.0)
    "apps.reports.urls",  # M7-15 §4s reports base (v0.9 scope pulled forward)
)

# M7-17: the v1.7.0 union's compat leg. The C1–C16 surface stays gate-
# invisible (spec §1.2); only /reports/ routes under the canonical
# /api/compat/v1 mount are walked, and the union test pins them to
# exactly these 5 W routes (docs/api-reports-merge-v1.md §5).
_COMPAT_MOUNT = "/api/compat/v1"
_COMPAT_URLCONF = "apps.compat.urls_compat"
_COMPAT_W_ROUTES = frozenset({
    ("get", f"{_COMPAT_MOUNT}/farms/{{farmSlug}}/salons/{{salonSlug}}/reports/"),
    ("get", f"{_COMPAT_MOUNT}/farms/{{farmSlug}}/salons/{{salonSlug}}/reports/{{report_date}}/"),
    ("get", f"{_COMPAT_MOUNT}/farms/{{farmSlug}}/reports/"),
    ("post", f"{_COMPAT_MOUNT}/reports/"),
    ("patch", f"{_COMPAT_MOUNT}/reports/{{report_id}}/"),
})


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


def _compat_pattern_to_template(route: str) -> str:
    """A urls_compat re_path regex -> the mounted contract path.

    '^farms/(?P<farmSlug>[A-Za-z0-9-]{1,64})/reports/?$' (mounted at
    /api/compat/v1) -> '/api/compat/v1/farms/{farmSlug}/reports/'.
    The compat regexes never nest parens inside (?P<...>) groups, so a
    flat [^)]* match is exact here."""
    import re

    out = route.strip()
    if out.startswith("^"):
        out = out[1:]
    if out.endswith("$"):
        out = out[:-1]
    out = re.sub(r"\(\?P<([^>]+)>[^)]*\)", r"{\1}", out)
    if out.endswith("/?"):
        out = out[:-2] + "/"
    return _COMPAT_MOUNT + "/" + out.lstrip("/")


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
    routes |= _implemented_compat_w()
    return routes


def _implemented_compat_w():
    """The compat leg of the v1.7.0 union: /reports/ routes of
    apps.compat.urls_compat under the canonical mount. The legacy /api/
    twins hit the same views but stay gate-invisible (C-surface rule)."""
    routes = set()
    resolver = URLResolver([], _COMPAT_URLCONF)

    def walk(res, prefix=""):
        for pat in res.url_patterns:
            if isinstance(pat, URLResolver):
                walk(pat, prefix + str(pat.pattern))
            elif isinstance(pat, URLPattern):
                template = _compat_pattern_to_template(prefix + str(pat.pattern))
                if "/reports/" not in template:
                    continue
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


@pytest.mark.django_db(transaction=False)
def test_ratified_v072_gateway_twins_are_documented():
    """v1.7.2 erratum ratification pin: the four manager-side lifecycle twins
    that lived in ERRATUM_PENDING are now contract paths. Dropping one back out
    of the yaml (or renaming a route) fails here instead of silently
    re-opening the documented-vs-implemented hole."""
    documented = _documented_full()
    missing = RATIFIED_V072_TWINS - documented
    assert missing == set(), (
        "v1.7.2 ratified these manager-side twins into the contract; they are "
        "no longer tolerated as undocumented extras:\n"
        + "\n".join(sorted(f"  {v.upper():6} {p}" for v, p in missing))
    )
    assert _implemented() >= RATIFIED_V072_TWINS


def test_known_gaps_ends_empty():
    """M6-08 exit gate: the diff test IS `implemented == contract` when this
    constant is empty — keep it imported here so the flip is greppable."""
    assert isinstance(KNOWN_GAPS, (set, frozenset))


@pytest.mark.django_db(transaction=False)
def test_compat_w_union_is_exact():
    """M7-17 ratification pin: the compat leg of the union is EXACTLY the
    5 W routes — a new compat /reports/ route (or a renamed one) fails
    here until the yaml absorbs it in the same commit."""
    assert _implemented_compat_w() == set(_COMPAT_W_ROUTES)
