#!/usr/bin/env python
"""
check_rls_parity.py — three-way RLS consistency guard (TASKS M1-06).

فارسی: این اسکریپت مطابقت سه‌گانه را چک می‌کند؛ اگر جدولی جا بیفتد یا اضافه
شود با کد غیرصفر شکست می‌خورد تا هیچ انحرافی از داکیومت بی‌صدا رد نشود.

Sources that MUST agree on the tenant-scoped table set:
  1. backend_docs/database.md        — every CREATE TABLE carrying org_id
  2. initdb/02_rls.sql               — the ARRAY[...] bootstrap list
  3. tenants/0002_rls_policies.py    — the frozen in-migration copy
     + the two hypertables whose RLS lives in telemetry/0002 (C-11)

Usage:
  python scripts/check_rls_parity.py [path/to/database.md]

Exit codes: 0 = all three agree; 1 = drift (details printed).
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DOC = Path(r"M:/projects/poultry/backend_docs/database.md")
FALLBACK_DOCS = [
    REPO.parent / "poultry" / "backend_docs" / "database.md",
    DEFAULT_DOC,
]
HYPERTABLES_RLS_IN_TELEMETRY = {"saloon_telemetry", "metric_hourly_aggregates"}


def doc_tenant_tables(doc_path: Path) -> set[str]:
    """Tables in database.md whose DDL block contains an org_id column."""
    text = doc_path.read_text(encoding="utf-8")
    tables: set[str] = set()
    for m in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\);", text, re.S):
        name, body = m.group(1), m.group(2)
        if re.search(r"^\s*org_id\b", body, re.M):
            tables.add(name)
    return tables


def initdb_rls_list() -> set[str]:
    text = (REPO / "initdb" / "02_rls.sql").read_text(encoding="utf-8")
    block = re.search(r"ARRAY\[(.*?)\];", text, re.S).group(1)
    # Hypertables are absent from the bootstrap list BY DESIGN (C-11: their
    # RLS ships inside telemetry/0002 before any columnstore setup).
    return set(re.findall(r"'(\w+)'", block)) | HYPERTABLES_RLS_IN_TELEMETRY


def migration_rls_list() -> set[str]:
    text = (REPO / "backend" / "apps" / "tenants" / "migrations" / "0002_rls_policies.py").read_text(encoding="utf-8")
    block = re.search(r"ARRAY\[(.*?)\];", text, re.S).group(1)
    return set(re.findall(r"'(\w+)'", block)) | HYPERTABLES_RLS_IN_TELEMETRY


def main() -> int:
    doc_path = Path(sys.argv[1]) if len(sys.argv) > 1 else next(p for p in FALLBACK_DOCS if p.exists())
    expected = doc_tenant_tables(doc_path)
    sources = {
        f"initdb/02_rls.sql": initdb_rls_list(),
        "tenants/0002_rls_policies.py (+hypertables)": migration_rls_list(),
    }
    ok = True
    print(f"database.md org-scoped tables: {len(expected)}")
    for label, actual in sources.items():
        missing, extra = expected - actual, actual - expected
        if missing or extra or len(actual) != len(expected):
            ok = False
            print(f"DRIFT vs {label}: missing={sorted(missing)} extra={sorted(extra)} ({len(actual)} entries)")
        else:
            print(f"OK vs {label}: {len(actual)} tables, 1:1 match")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
