"""
Shared pytest fixtures.

ÙØ§Ø±Ø³ÛŒ: ÙÛŒÚ©Ø³Ú†Ø±Ù‡Ø§ÛŒ Ù…Ø´ØªØ±Ú©Ø› Ø§Ø³ØªØ«Ù†Ø§ÛŒÙ --nomigrations Ø¨Ø±Ø§ÛŒ ØªØ³Øªâ€ŒÙ‡Ø§ÛŒ RLS/Ø¨Ø°Ø± Ø§ÛŒÙ†Ø¬Ø§Ø³Øª
ØªØ§ Ø³ÛŒØ§Ø³Øªâ€ŒÙ‡Ø§ Ùˆ Ø¯Ø§Ø¯Ù‡â€ŒÙ‡Ø§ÛŒ Ø¨Ø°Ø±ÛŒ ÙˆØ§Ù‚Ø¹Ø§Ù‹ ÙˆØ¬ÙˆØ¯ Ø¯Ø§Ø´ØªÙ‡ Ø¨Ø§Ø´Ù†Ø¯ Ùˆ ØªØ³Øªâ€ŒÙ‡Ø§ ØªÙˆØ®Ø§Ù„ÛŒ Ø³Ø¨Ø² Ù†Ø´ÙˆÙ†Ø¯
(Ø®Ø·Ø± vacuous-pass â€” TUTORIAL Â§6).

Why this exists: pytest.ini runs with --nomigrations (tables synthesized from
model state = fast), which means RunSQL migration payloads (RLS policies,
seeds) NEVER execute in tests. The Blueprint Â§7 answer is an explicit
exception: postgres-mode sessions apply the very same SQL artifacts through
the `seeded_rls_db` fixture below. Sources stay single: initdb/*.sql and the
seed migrations themselves â€” nothing duplicated.
"""

import importlib.util
import os
import re
from pathlib import Path

import pytest
from django.db import connection


def redis_base_url() -> str:
    """Env-provided Redis URL WITHOUT any logical-db suffix (CI vs dev aware)."""
    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return re.sub(r"/\d+$", "", url)


def __getattr__(name):
    if name == 'INTEGRATION_REDIS_URL':
        return redis_base_url() + '/11'
    raise AttributeError(name)


BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
# Repo-root machinery (roles + RLS bootstrap SQL). In CI the image packages
# only ./backend, so compose mounts ./initdb read-only and points this env var
# at it â€” the SQL itself is never duplicated.
INITDB_DIR = Path(os.environ.get("MORGHDARI_INITDB_DIR", REPO_ROOT / "initdb"))

# Seed payload lives INSIDE these migrations â€” import them by path (their
# module names start with digits, so importlib-by-path is required).
SEED_MIGRATIONS = [
    BACKEND_DIR / "apps" / "telemetry" / "migrations" / "0003_seed_metric_types.py",
    BACKEND_DIR / "apps" / "system_config" / "migrations" / "0003_seed_configuration_and_quotas.py",
    BACKEND_DIR / "apps" / "system_config" / "migrations" / "0004_alert_tunables.py",
    BACKEND_DIR / "apps" / "geography" / "migrations" / "0002_seed_geo_sample.py",
]


def _sql_from_migration(path: Path) -> str:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SQL


_UNMANAGED_TABLES = ("saloon_telemetry", "metric_hourly_aggregates")


def _install_cascade_flush(connection) -> None:
    """Django builds TRUNCATE lists from MANAGED models only; our TimescaleDB
    hypertables are unmanaged yet reference managed tables â€” without this the
    per-test flush dies on FeatureNotSupported once ANY test writes a
    hypertable row. Force-CASCADE plus the explicit unmanaged list only WIDENS
    what the framework already decided to truncate. Installed per session and
    left installed (monkeypatch-style auto-undo fires BEFORE DB teardown,
    which is exactly the wrong moment).
    ÙØ§Ø±Ø³ÛŒ: Ù¾Ú† Ø¯Ø± Ø³Ø·Ø­ Ù†Ø´Ø³Øª Ù…ÛŒâ€ŒÙ…Ø§Ù†Ø¯ Ú†ÙˆÙ† Ø­Ø°ÙÙ Ø²ÙˆØ¯Ù‡Ù†Ú¯Ø§Ù…Ø´ Ù¾ÛŒØ´ Ø§Ø² Ù¾Ø§Ú©â€ŒØ³Ø§Ø²ÛŒ Ø±Ø® Ù…ÛŒâ€ŒØ¯Ù‡Ø¯.
    """
    if getattr(connection.ops, "_morghdari_cascade_patch", False):
        return
    original = connection.ops.sql_flush

    def with_unmanaged(style, tables, *args, **kwargs):
        # Include unmanaged hypertables ONLY when they actually exist: modules
        # that never request the timescale fixture otherwise die in teardown
        # on fresh --create-db sessions purely due to module ordering.
        candidates = list(dict.fromkeys([*tables, *_UNMANAGED_TABLES]))
        with connection.cursor() as cursor:
            existing = [
                t for t in candidates
                if cursor.execute(
                    "SELECT to_regclass('public.' || quote_ident(%s)) IS NOT NULL", (t,)
                ).fetchone()[0]
            ]
        kwargs["allow_cascade"] = True
        return original(style, existing, *args, **kwargs)

    connection.ops.sql_flush = with_unmanaged
    connection.ops._morghdari_cascade_patch = True


@pytest.fixture(scope="session")
def seeded_rls_db(django_db_setup, django_db_blocker):
    """Complete the --nomigrations schema for postgres sessions.

    Applies, in order: roles (idempotent guards), RLS ENABLE/FORCE/policies
    over every existing org table (hypertables included when present),
    metric/config/quota/geo seeds (ON CONFLICT DO NOTHING).

    Skips entirely on sqlite laptop loops â€” integration truth lives in CI
    (config/settings/test.py docstring).
    """
    with django_db_blocker.unblock():
        if connection.vendor != "postgresql":
            pytest.skip("RLS + seed truth requires PostgreSQL/TimescaleDB (CI sets DATABASE_URL)")
        _install_cascade_flush(connection)
        with connection.cursor() as cursor:
            # Roles first: 02_rls FORCE + policy application needs no special
            # role, but keeping order identical to a production bootstrap makes
            # failures here mirror bootstrap failures. All scripts idempotent.
            cursor.execute((INITDB_DIR / "01_roles.sql").read_text(encoding="utf-8"))
            cursor.execute((INITDB_DIR / "02_rls.sql").read_text(encoding="utf-8"))
            # Test-ordering remediation ONLY: in a real bootstrap the
            # default privileges above precede table creation; here tables
            # already exist (syncdb ran first), so grant them explicitly.
            # Production paths are untouched.
            cursor.execute(
                "GRANT USAGE ON SCHEMA public TO morghdari_app;"
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO morghdari_app;"
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO morghdari_app;"
            )
            for path in SEED_MIGRATIONS:
                cursor.execute(_sql_from_migration(path))
    yield


@pytest.fixture(scope="session")
def timescale_db(seeded_rls_db, django_db_blocker):
    """Unmanaged TimescaleDB objects for drain/integration tests (M3-05+).

    saloon_telemetry / metric_hourly_aggregates are managed=False: syncdb never
    creates them and --nomigrations hides the RunSQL migration that would. The
    single source of truth for their DDL is THAT migration file, so we replay
    its SQL payload by path (same loader as seeds). Extension is instance-wide
    preloaded; CREATE EXTENSION IF NOT EXISTS keeps this idempotent.
    ÙØ§Ø±Ø³ÛŒ: Ø¬Ø¯ÙˆÙ„â€ŒÙ‡Ø§ÛŒ Ø§ÙŽÙ†â€ŒÙ…Ù†ÛŒØ¬Ù ØªØ§ÛŒÙ…â€ŒØ§Ø³Ú©ÛŒÙ„ Ø§Ø² Ø·Ø±ÛŒÙ‚ Ù‡Ù…ÛŒÙ† ÙØ§ÛŒÙ„ Ù…Ù‡Ø§Ø¬Ø±Øª Ø³Ø§Ø®ØªÙ‡ Ù…ÛŒâ€ŒØ´ÙˆÙ†Ø¯ â€”
    ÛŒÚ© Ù…Ù†Ø¨Ø¹ØŒ Ø¨Ø¯ÙˆÙ† Ú©Ù¾ÛŒÙ DDL.
    """
    with django_db_blocker.unblock():
        if connection.vendor != "postgresql":
            pytest.skip("hypertable truth requires PostgreSQL/TimescaleDB")
        _install_cascade_flush(connection)
        with connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            already = cursor.execute(
                "SELECT to_regclass('public.saloon_telemetry') IS NOT NULL"
            ).fetchone()[0]
            if not already:
                cursor.execute(
                    _sql_from_migration(
                        BACKEND_DIR
                        / "apps"
                        / "telemetry"
                        / "migrations"
                        / "0002_timescale_hypertables.py"
                    )
                )
    yield

