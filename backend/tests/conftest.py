"""
Shared pytest fixtures.

فارسی: فیکسچرهای مشترک؛ استثنایِ --nomigrations برای تست‌های RLS/بذر اینجاست
تا سیاست‌ها و داده‌های بذری واقعاً وجود داشته باشند و تست‌ها توخالی سبز نشوند
(خطر vacuous-pass — TUTORIAL §6).

Why this exists: pytest.ini runs with --nomigrations (tables synthesized from
model state = fast), which means RunSQL migration payloads (RLS policies,
seeds) NEVER execute in tests. The Blueprint §7 answer is an explicit
exception: postgres-mode sessions apply the very same SQL artifacts through
the `seeded_rls_db` fixture below. Sources stay single: initdb/*.sql and the
seed migrations themselves — nothing duplicated.
"""

import importlib.util
from pathlib import Path

import pytest
from django.db import connection

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

# Seed payload lives INSIDE these migrations — import them by path (their
# module names start with digits, so importlib-by-path is required).
SEED_MIGRATIONS = [
    BACKEND_DIR / "apps" / "telemetry" / "migrations" / "0003_seed_metric_types.py",
    BACKEND_DIR / "apps" / "system_config" / "migrations" / "0003_seed_configuration_and_quotas.py",
    BACKEND_DIR / "apps" / "geography" / "migrations" / "0002_seed_geo_sample.py",
]


def _sql_from_migration(path: Path) -> str:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SQL


@pytest.fixture(scope="session")
def seeded_rls_db(django_db_setup, django_db_blocker):
    """Complete the --nomigrations schema for postgres sessions.

    Applies, in order: roles (idempotent guards), RLS ENABLE/FORCE/policies
    over every existing org table (hypertables included when present),
    metric/config/quota/geo seeds (ON CONFLICT DO NOTHING).

    Skips entirely on sqlite laptop loops — integration truth lives in CI
    (config/settings/test.py docstring).
    """
    with django_db_blocker.unblock():
        if connection.vendor != "postgresql":
            pytest.skip("RLS + seed truth requires PostgreSQL/TimescaleDB (CI sets DATABASE_URL)")
        with connection.cursor() as cursor:
            # Roles first: 02_rls FORCE + policy application needs no special
            # role, but keeping order identical to a production bootstrap makes
            # failures here mirror bootstrap failures. All scripts idempotent.
            cursor.execute((REPO_ROOT / "initdb" / "01_roles.sql").read_text(encoding="utf-8"))
            cursor.execute((REPO_ROOT / "initdb" / "02_rls.sql").read_text(encoding="utf-8"))
            for path in SEED_MIGRATIONS:
                cursor.execute(_sql_from_migration(path))
    yield
