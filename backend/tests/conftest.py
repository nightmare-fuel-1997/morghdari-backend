"""Shared pytest fixtures for local and PostgreSQL integration runs."""

import importlib.util
import os
import re
from pathlib import Path

import pytest
from django.db import connection


def redis_base_url() -> str:
    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    # Test modules use host-local scratch DBs; Docker CI must resolve Redis by
    # service name. Keep the password and strip only the logical DB suffix.
    return re.sub(r"/\d+$", "", url)


INTEGRATION_REDIS_URL = redis_base_url() + "/11"
BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
INITDB_DIR = Path(os.environ.get("MORGHDARI_INITDB_DIR", REPO_ROOT / "initdb"))
SEED_MIGRATIONS = [
    BACKEND_DIR / "apps" / "telemetry" / "migrations" / "0003_seed_metric_types.py",
    BACKEND_DIR / "apps" / "system_config" / "migrations" / "0003_seed_configuration_and_quotas.py",
    BACKEND_DIR / "apps" / "system_config" / "migrations" / "0004_alert_tunables.py",
    BACKEND_DIR / "apps" / "system_config" / "migrations" / "0005_los_suppress_tunable.py",
    BACKEND_DIR / "apps" / "geography" / "migrations" / "0002_seed_geo_sample.py",
]
_EXPECTED_REFERENCE_COUNTS: dict = {}
_UNMANAGED_TABLES = ("saloon_telemetry", "metric_hourly_aggregates")
_REFERENCE_TABLES = ("system_configuration", "tier_quota_mapping", "sensor_metric_types")


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, _script, _numkeys, key, token):
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0

    def delete(self, *keys):
        return sum(self.values.pop(key, None) is not None for key in keys)


@pytest.fixture
def fake_redis():
    return FakeRedis()


def _sql_from_migration(path: Path) -> str:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SQL


def _execute_sql(cursor, sql: str) -> None:
    if connection.vendor != "sqlite":
        cursor.execute(sql)
        return
    if "JOIN (VALUES" in sql:
        return
    statement = []
    quoted = False
    for char in sql:
        if char == "'":
            quoted = not quoted
        if char == ";" and not quoted:
            text = "".join(statement).strip()
            if text:
                cursor.execute(text)
            statement = []
        else:
            statement.append(char)
    text = "".join(statement).strip()
    if text:
        cursor.execute(text)


def _install_cascade_flush(conn):
    if getattr(conn.ops, "_morghdari_cascade_patch", False):
        return
    original = conn.ops.sql_flush

    def with_unmanaged(style, tables, *args, **kwargs):
        candidates = [t for t in dict.fromkeys([*tables, *_UNMANAGED_TABLES]) if t not in _REFERENCE_TABLES]
        if conn.vendor == "sqlite":
            # Local-parity fix (2026-09-09, M7-07 wave): the old silent no-op
            # made transaction=True suites (M2-10 matrix) pass once and then
            # UNIQUE-collide on every later case under --reuse-db, diverging
            # from CI's real PG flush. Emit DELETEs for tables that exist;
            # the unmanaged hypertables never exist on sqlite, so the
            # existence filter drops them naturally.
            with conn.cursor() as cursor:
                existing = {
                    row[0]
                    for row in cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                        " AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
            candidates = [t for t in candidates if t in existing]
        elif conn.vendor == "postgresql":
            with conn.cursor() as cursor:
                candidates = [t for t in candidates if cursor.execute("SELECT to_regclass('public.' || quote_ident(%s)) IS NOT NULL", (t,)).fetchone()[0]]
        kwargs["allow_cascade"] = True
        return original(style, candidates, *args, **kwargs)

    conn.ops.sql_flush = with_unmanaged
    conn.ops._morghdari_cascade_patch = True


def _reference_seed_counts(cursor):
    return {table: cursor.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in _REFERENCE_TABLES}


@pytest.fixture(scope="session")
def seeded_rls_db(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        _install_cascade_flush(connection)
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute((INITDB_DIR / "01_roles.sql").read_text(encoding="utf-8"))
                cursor.execute((INITDB_DIR / "02_rls.sql").read_text(encoding="utf-8"))
                # PRIVATE FRESH test DBs: roles/policies above are cluster/DB
                # scoped, but TABLE grants reach Django's tables only through
                # ALTER DEFAULT PRIVILEGES (future tables) or this explicit
                # grant (ALL tables). initdb ran 01_roles.sql BEFORE Django
                # created any table, so a brand-new test DB has app-role
                # grants NOWHERE until this runs. Idempotent — safe to apply
                # unconditionally at session setup (and again per-test when
                # the starvation repair fires).
                cursor.execute(
                    "GRANT USAGE ON SCHEMA public TO morghdari_app;"
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO morghdari_app;"
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO morghdari_app;"
                )
                for path in SEED_MIGRATIONS:
                    _execute_sql(cursor, _sql_from_migration(path))
                _EXPECTED_REFERENCE_COUNTS["counts"] = _reference_seed_counts(cursor)
        else:
            from apps.geography.models import City, Province, Village
            from apps.system_config.models import SystemConfiguration, TierQuotaMapping
            from apps.telemetry.models import SensorMetricType
            provinces = [Province.objects.create(name=name) for name in ("Isfahan", "Tehran", "Razavi Khorasan")]
            city = City.objects.create(province=provinces[0], name="Kashan")
            City.objects.create(province=provinces[1], name="Tehran")
            City.objects.create(province=provinces[2], name="Mashhad")
            for index in range(3):
                Village.objects.create(city=city, name=f"Seed Village {index}")
            metrics = {"temp": ("Temperature", "C", True), "humidity": ("Humidity", "%", False), "lux": ("Light Intensity", "lux", False), "nh3": ("Ammonia", "ppm", True), "water_l": ("Water Intake", "L", False), "co": ("Carbon Monoxide", "ppm", True), "power_mains": ("Mains Power", "bool", False), "power_gen": ("Generator", "bool", False)}
            for metric_key, (display, unit, tier1) in metrics.items():
                SensorMetricType.objects.create(metric_key=metric_key, display_name=display, unit=unit, value_type="numeric", is_tier1=tier1)
            config_keys = ("replay_rate_limit_per_min", "org_offboard_grace_days", "replay_rate_limit_per_org_per_min", "quota_exempt_tier1_metrics", "dedup_window_sec", "los_watchdog_interval_sec", "clock_drift_alert_threshold_min", "threshold_transition_grace_sec", "cmd_timeout_ttl_sec", "nh3_max_ts_velocity_threshold", "telemetry_insert_batch_size", "subscriber_mqueue_len", "hysteresis_buffer_units", "active_flock_temp_tolerance_c", "dlq_alert_threshold", "alert_acknowledgement_pause_minutes", "ws_max_connections_per_user", "mqtt_cert_renewal_interval_days", "max_pending_gateway_requests", "los_suppress_after_redis_restart_min", "db_connections_alert_pct", "temp_velocity_delta_60s", "metric_autodiscover_min_windows", "farm_power_outage_los_ratio", "farm_power_outage_window_min", "correlation_suppress_minutes", "breach_duration_sec", "sensor_status_ttl_sec", "escalation_tier2_min", "escalation_tier3_min")
            for key in config_keys:
                SystemConfiguration.objects.create(key=key, value="90000" if key == "dedup_window_sec" else "10")
            for values in (("basic", 5, 20, 50, 10000, 2), ("pro", 20, 100, 250, 50000, 10), ("enterprise", 9999, 9999, 9999, 999999, 9999)):
                TierQuotaMapping.objects.create(tier=values[0], max_farms=values[1], max_saloons=values[2], max_sensor_slots=values[3], max_hourly_messages=values[4], max_gateways=values[5])
    yield


@pytest.fixture(autouse=True)
def _reference_seed_guard(request):
    """C-16 containment: verify-or-reseed the reference catalogs per test.

    On PostgreSQL sessions the seeded catalogs (system_configuration /
    tier_quota_mapping / sensor_metric_types) were observed EMPTIED
    mid-session by an execution channel no instrument could attribute (full
    statement audit, FK-graph analysis and DB-identity checks all came back
    clean while seeds verifiably commit). The flush exclusion in
    _install_cascade_flush narrows the py path; this guard closes the CLASS
    of channel entirely: before each test on PostgreSQL, cheaply COUNT the
    catalogs; any starvation is repaired from the SAME idempotent seed
    migrations (ON CONFLICT DO NOTHING) before the test body runs. Tests can
    therefore rely on seeded truth unconditionally, and config-driven
    behavior (D21) is testable against real DB values instead of code-level
    fallbacks.
    فارسی: پیش از هر تستِ پستگرس، شمارشِ کاتالوگ‌ها کنترل و در صورت کمبود، از
    همین مهاجرت‌های بذریِ تکرارپذیر ترمیم می‌شود تا رفتارِ وابسته به پیکربندی
    همیشه با مقادیرِ واقعی دیتابیس آزموده شود.
    """
    if request.node.get_closest_marker("django_db") is None:
        # Pure-Redis/non-DB tests must never touch the blocked connection.
        yield
        return
    if connection.vendor != "postgresql":
        yield
        return

    with connection.cursor() as cursor:
        missing_setup = cursor.execute(
            "SELECT to_regclass('public.system_configuration') IS NULL"
            " OR to_regclass('public.sensor_metric_types') IS NULL"
        ).fetchone()[0]
        if not missing_setup:
            counts = _reference_seed_counts(cursor)
            expected = _EXPECTED_REFERENCE_COUNTS.get("counts")
            starved = (
                expected is None
                or counts["system_configuration"] < expected["system_configuration"]
                or counts["sensor_metric_types"] < expected["sensor_metric_types"]
                or counts["tier_quota_mapping"] < expected["tier_quota_mapping"]
            )
        else:
            starved = True

    if starved:
        # Deterministic commit boundary: repairs must land even when the
        # surrounding fixture state is mid-transaction.
        from django.db import transaction as dj_tx

        with dj_tx.atomic():
            with connection.cursor() as repair_cursor:
                if missing_setup:
                    repair_cursor.execute(
                        (INITDB_DIR / "01_roles.sql").read_text(encoding="utf-8")
                    )
                    repair_cursor.execute(
                        (INITDB_DIR / "02_rls.sql").read_text(encoding="utf-8")
                    )
                    repair_cursor.execute(
                        "GRANT USAGE ON SCHEMA public TO morghdari_app;"
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO morghdari_app;"
                        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO morghdari_app;"
                    )
                for path in SEED_MIGRATIONS:
                    repair_cursor.execute(_sql_from_migration(path))
        with connection.cursor() as verify_cursor:
            counts = _reference_seed_counts(verify_cursor)
        _EXPECTED_REFERENCE_COUNTS["counts"] = counts

    yield


@pytest.fixture(scope="session")
def django_db_setup(
    request,
    django_test_environment,
    django_db_blocker,
    django_db_use_migrations,
    django_db_keepdb,
    django_db_createdb,
    django_db_modify_db_settings,
):
    """pytest-django's django_db_setup (same contract, ordering and flags)
    PLUS the `use_migrations` marker's SQL artifacts.

    WHY the override (F2, 2026-09-10 v0.7 re-verify): the marker was declared
    in pytest.ini but implemented NOWHERE, so a COLD test DB — created by
    syncdb under --nomigrations — never received the reports app's UNMANAGED
    tables (daily_reports & co. exist ONLY as reports/0001-0002 RunSQL DDL),
    and all 45 reports tests failed with `relation "daily_reports" does not
    exist`. Warm/`--reuse-db` runs masked it (the DoD's 513-green sat on a
    DB polluted by dev-time migrates). The RLS policies, hypertables and
    reference seeds the marker exists for are ALREADY materialized on every
    PG session by the seeded_rls_db / timescale_db fixtures — the reports
    DDL was the one artifact nothing provided, so this override materializes
    it (idempotent RunSQL: to_regclass guards + DROP POLICY IF EXISTS) right
    after the test DB is created, restoring the documented marker contract on
    a truly fresh database.
    فارسی: پیاده‌سازی مارکر use_migrations — جدول‌های unmanaged گزارش‌ها روی
    دیتابیس تستِ خام ساخته می‌شوند تا گیت از حالت سرد سبز بماند.
    """
    from django.test.utils import setup_databases, teardown_databases
    from pytest_django.fixtures import _disable_migrations, _get_databases_for_setup

    setup_databases_args = {}
    if not django_db_use_migrations:
        _disable_migrations()
    if django_db_keepdb and not django_db_createdb:
        setup_databases_args["keepdb"] = True
    aliases, serialized_aliases = _get_databases_for_setup(request.session.items)

    with django_db_blocker.unblock():
        db_cfg = setup_databases(
            verbosity=request.config.option.verbose,
            interactive=False,
            aliases=aliases,
            serialized_aliases=serialized_aliases,
            **setup_databases_args,
        )
        _materialize_use_migrations_schema(request)
        _materialize_compat_schema()

    yield

    if not django_db_keepdb:
        with django_db_blocker.unblock():
            try:
                teardown_databases(db_cfg, verbosity=request.config.option.verbose)
            except Exception as exc:  # noqa: BLE001
                request.node.warn(
                    pytest.PytestWarning(
                        f"Error when trying to teardown test databases: {exc!r}"
                    )
                )


def _materialize_compat_schema() -> None:
    """Create `legacy_slug_map` on a fresh test DB (2026-10-20).

    WHY this is NOT gated on the `use_migrations` marker, unlike the reports
    tables above: `apps.compat.models.LegacySlugMap` is `managed = False`, so
    syncdb under `--nomigrations` skips it, and its table exists only as RunSQL
    in `compat/0001_legacy_slug_map`. But `mint_saloon_slug()` runs on EVERY
    saloon creation, which is core topology — not an optional reports feature.
    Without this, creating a saloon over the API fails on Postgres with
    `relation "legacy_slug_map" does not exist`, which is exactly how this was
    found: the role-widening tests (task 2.16) were the first to create a saloon
    through the view under CI's cold DB. sqlite (local loop) skips, matching the
    pragmatism of the other materialisers.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        if cursor.execute(
            "SELECT to_regclass('public.legacy_slug_map') IS NOT NULL"
        ).fetchone()[0]:
            return
        # compat/0001 exposes a module-level `SQL` constant (not the reports
        # `_sql()` function shape), so load it directly.
        spec = importlib.util.spec_from_file_location(
            "compat_legacy_slug_map",
            BACKEND_DIR / "apps" / "compat" / "migrations" / "0001_legacy_slug_map.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _execute_sql(cursor, module.SQL)

    # `metric_daily_aggregates`, for the same reason and with the same shape.
    #
    # MetricDailyAggregate is managed=False with a RESTRICT FK to Saloon, so
    # Django's delete collector queries the table whenever a saloon is deleted
    # — core topology, not an optional rollup feature. Under --nomigrations the
    # table exists only as RunSQL in monitoring/0002, so every farm/saloon
    # delete test failed on a cold DB with `relation "metric_daily_aggregates"
    # does not exist`. It was already materialized, but only behind the
    # `use_migrations` marker, which those tests do not carry.
    with connection.cursor() as cursor:
        if cursor.execute(
            "SELECT to_regclass('public.metric_daily_aggregates') IS NOT NULL"
        ).fetchone()[0]:
            return
        _execute_sql(
            cursor,
            _reports_ddl(
                BACKEND_DIR / "apps" / "monitoring" / "migrations" / "0002_daily_aggregates.py"
            ),
        )


def _materialize_use_migrations_schema(request) -> None:
    """Create the `use_migrations` marker's SQL artifacts on a fresh test DB.

    Only the reports app needs this: its models are managed=False and their
    tables exist solely as RunSQL inside reports/0001-0002, so syncdb skips
    them and a cold DB has no daily_reports (F2). The migration SQL is
    written idempotently (to_regclass guards, DROP POLICY IF EXISTS) — safe
    to execute directly, exactly like the timescale_db fixture does for
    telemetry/0002. Non-Postgres (sqlite laptop loop) and marker-less
    sessions skip — same pragmatism as the seeded fixtures.
    """
    if connection.vendor != "postgresql":
        return
    if not any(item.get_closest_marker("use_migrations") for item in request.session.items):
        return
    reports_dir = BACKEND_DIR / "apps" / "reports" / "migrations"
    with connection.cursor() as cursor:
        if not cursor.execute(
            "SELECT to_regclass('public.daily_reports') IS NOT NULL"
        ).fetchone()[0]:
            _execute_sql(cursor, _reports_ddl(reports_dir / "0001_initial.py"))
        if not cursor.execute(
            "SELECT to_regclass('public.daily_report_extras') IS NOT NULL"
        ).fetchone()[0]:
            _execute_sql(cursor, _reports_ddl(reports_dir / "0002_daily_report_extras.py"))



def _reports_ddl(path: Path) -> str:
    """Load a reports migration and return its DDL. Reports migrations expose
    `_sql()` (a function — connection.vendor is read at CALL time), unlike
    telemetry/0002 which ships a module-level `SQL` constant; the loader in
    _sql_from_migration only handles the latter. monitoring/0002 (batch-1
    aggregates) exposes the same `_sql()` function shape — reuse this loader."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._sql()


@pytest.fixture(scope="session")
def timescale_db(seeded_rls_db, django_db_blocker):
    with django_db_blocker.unblock():
        if connection.vendor == "postgresql":
            _install_cascade_flush(connection)
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                if not cursor.execute("SELECT to_regclass('public.saloon_telemetry') IS NOT NULL").fetchone()[0]:
                    _execute_sql(cursor, _sql_from_migration(BACKEND_DIR / "apps" / "telemetry" / "migrations" / "0002_timescale_hypertables.py"))
    yield


@pytest.fixture
def clean_sub_state():
    """The subscriber's SUBACK bookkeeping is module-global — each test starts blank.

    Shared (was local to `test_subscriber_boot.py`) since 2026-10-20: the
    device-lane tests in `test_m13_01_device_lanes.py` assert on the same
    `_granted_topics` set, and two private copies of a clear-the-globals fixture
    is how one of them silently stops clearing.
    """
    from morghdari.subscriber import main as _sub

    _sub._pending_subs.clear()
    _sub._granted_topics.clear()
    yield
    _sub._pending_subs.clear()
    _sub._granted_topics.clear()
