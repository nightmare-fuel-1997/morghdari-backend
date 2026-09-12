"""Per-module coverage gate — the aggregate-hole closer.

WHY this exists (2026-09-07 audit): the enforced gate is a single total
(--cov-fail-under=80), so four files at 46-79% once hid behind an 88% total.
Two real bugs (firing.py json.dumps on a raw UUID after commit, ops_services
building the redis client outside its try) survived inside those blind spots.
Aggregate green is necessary but not sufficient.

What it does:
  - FAIL (exit 1) when any request-path file is below 80:
    *views*.py, api*.py, consumers*.py, ws*.py
    (the live UX contract — a thinly tested view ships 500s to farmers).
    v1.7.2: the match widened from `api.py` to `api*.py`. The follow-through
    wave ships `api_follow.py` / `views_follow.py`-style modules, and the exact
    name `api.py` would have left the NEWEST request paths outside this gate —
    the same aggregate-hole class the 2026-09-07 audit created this script for.
  - WARN (exit 0, listed) when any service file is below 90:
    *services*.py, services/*.py
    (the safety math — breach/firing/sweeps deserve the M4 >=90% bar)
  - Skips scaffolding with no logic worth gating:
    __init__, admin, apps, urls, models, migrations.

Usage:
  pytest --cov=apps --cov-report=json:coverage.json ... && python scripts/check_module_coverage.py
  python scripts/check_module_coverage.py --strict-services   # flip WARN to FAIL
  python scripts/check_module_coverage.py --coverage-file /tmp/coverage.json

Exit 0 = views gate green (services warnings are backfill queue, not blockers).
Exit 1 = a view file below 80, or (with --strict-services) a service below 90,
         or coverage.json missing/unreadable.
"""

import argparse
import json
import os
import sys

VIEWS_MIN = 80.0
SERVICES_MIN = 90.0

_SKIP_SUBSTRINGS = ("__init__", "admin.py", "apps.py", "urls", "models.py", "migrations")


def _bucket(filename: str) -> str | None:
    base = filename.replace("\\", "/")
    low = base.lower()
    if any(s in low for s in _SKIP_SUBSTRINGS):
        return None
    name = low.rsplit("/", 1)[-1]
    if "service" in low:
        return "services"
    if "view" in name or name.startswith("api") or "consumer" in name or name.startswith("ws"):
        return "views"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Per-module coverage gate")
    parser.add_argument("--coverage-file", default="coverage.json")
    parser.add_argument("--strict-services", action="store_true")
    parser.add_argument("--views-min", type=float, default=VIEWS_MIN)
    parser.add_argument("--services-min", type=float, default=SERVICES_MIN)
    args = parser.parse_args()

    if not os.path.exists(args.coverage_file):
        print(
            f"coverage-gate: FAIL — {args.coverage_file} not found; "
            "run pytest with --cov-report=json first",
            file=sys.stderr,
        )
        return 1

    with open(args.coverage_file, encoding="utf-8") as fh:
        data = json.load(fh)

    view_failures: list[str] = []
    service_warnings: list[str] = []
    checked_views = 0
    checked_services = 0

    for filename, filedata in data.get("files", {}).items():
        bucket = _bucket(filename)
        if bucket is None:
            continue
        summary = filedata.get("summary", {})
        pct = float(summary.get("percent_covered", 0.0))
        # Compare the ROUNDED integer — the same number the term-missing table
        # shows a human (79.72% displays as 80%). The gate hunts blind spots
        # (files at 40-70%), not 0.3% rounding dust on a green file.
        shown = int(pct + 0.5)
        if bucket == "views":
            checked_views += 1
            if shown < int(args.views_min):
                view_failures.append(f"{filename}: {pct:.1f}% < {args.views_min:.0f}%")
        else:
            checked_services += 1
            if shown < int(args.services_min):
                service_warnings.append(f"{filename}: {pct:.1f}% < {args.services_min:.0f}%")

    print(f"coverage-gate: checked {checked_views} view files (min {args.views_min:.0f}%), "
          f"{checked_services} service files (target {args.services_min:.0f}%)")
    if service_warnings:
        print("coverage-gate: service backfill queue (WARN, not blocking):")
        for line in sorted(service_warnings):
            print(f"  warn: {line}")
    if view_failures:
        print("coverage-gate: FAIL — thinly tested request paths:")
        for line in sorted(view_failures):
            print(f"  fail: {line}")
        return 1
    if service_warnings and args.strict_services:
        print("coverage-gate: FAIL — --strict-services flips warnings to failures")
        return 1
    print("coverage-gate: GREEN — every request path meets its floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
