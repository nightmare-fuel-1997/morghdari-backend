"""
test_rls.py — M1 EXIT GATE (TASKS M1-09).

فارسی: گیتِ خروجِ M1 — اثبات رسمی جداسازی تنانت روی پایگاه‌دادهٔ واقعی؛ سبز شدن
این فایل در CI یعنی v0.1 از نظر امنیتیِ دیتابیس تمام است.

The four mandated assertions, against REAL TimescaleDB with FORCE'd RLS:
  1. cross-org SELECT returns 0 rows
  2. cross-org INSERT is rejected by WITH CHECK
  3. unset GUC ⇒ 0 rows everywhere (fail-closed)
  4. no GUC leakage between transactions on a pooled connection

Hypertable note (C-11 Approach B): saloon_telemetry / metric_hourly_aggregates
are managed=False and do not exist under --nomigrations table synthesis; their
DDL ships inside telemetry/0002. metric_hourly_aggregates retains ENABLE +
FORCE ROW LEVEL SECURITY, whereas saloon_telemetry disables PostgreSQL RLS so
TimescaleDB columnstore compression + add_compression_policy can run, and is
protected instead by fail-closed TenantSafeTelemetryManager + explicit org_id
predicates + parent saloons/farms RLS gatekeepers.
scripts/check_rls_parity.py guards that the policy TABLE LIST and C-11
exemption set stay explicit.
"""

import pytest
from django.db import connection, transaction
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts.models import AccountRoleScope, OrgMember, User
from apps.accounts.serializers import resolve_active_membership
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.context import tenant_context
from apps.tenants.models import Organization

pytestmark = [
    pytest.mark.use_migrations,
    pytest.mark.django_db(transaction=True),
]


def _fetch(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _fetchone(sql, params=None):
    return _fetch(sql, params)[0][0]


def _build_org(tag):
    """One populated organization. Created via ORM as the bootstrap superuser
    (RLS never applies to it), then asserted via SET ROLE."""
    province = Province.objects.create(name=f"prov-{tag}")
    city = City.objects.create(province=province, name=f"city-{tag}")
    org = Organization.objects.create(name=f"org-{tag}")
    owner = User.objects.create_user(f"owner-{tag}", password="x")
    OrgMember.objects.create(org=org, user=owner, role="OWNER")
    farm = Farm.objects.create(org=org, name=f"farm-{tag}", province=province, city=city)
    s1 = Saloon.objects.create(org=org, farm=farm, name=f"saloon-1-{tag}")
    s2 = Saloon.objects.create(org=org, farm=farm, name=f"saloon-2-{tag}")
    return {"org": org, "farm": farm, "saloons": [s1, s2]}


def test_cross_org_select_is_zero(seeded_rls_db):
    a, b = _build_org("a"), _build_org("b")  # built as superuser, BEFORE the role switch
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")
    try:
        with transaction.atomic(), tenant_context(a["org"].org_id):
            # 1. Cross-org SELECT: org A must see exactly its own 2 saloons,
            #    zero of B's, regardless of table.
            assert _fetchone("SELECT count(*) FROM saloons") == 2
            assert _fetchone(
                "SELECT count(*) FROM saloons WHERE name LIKE 'saloon-%b'"
            ) == 0
            assert _fetchone("SELECT count(*) FROM farms") == 1
            assert _fetchone("SELECT id FROM farms") == a["farm"].id
            assert _fetchone("SELECT count(*) FROM org_members") == 1
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_cross_org_insert_blocked(seeded_rls_db):
    a, b = _build_org("a"), _build_org("b")
    b_farm_id = b["farm"].id
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")
    try:
        with transaction.atomic(), tenant_context(a["org"].org_id):
            # 2. WITH CHECK rejects writing into org B even while scoped to A.
            with pytest.raises(Exception):
                with transaction.atomic():  # savepoint isolates the violation
                    with connection.cursor() as c2:
                        c2.execute(
                            "INSERT INTO saloons (farm_id, org_id, name) VALUES (%s, %s, 'smuggled')",
                            [b_farm_id, str(b["org"].org_id)],
                        )
            # The row must NOT exist afterwards — not even for its "own" org.
            with transaction.atomic(), tenant_context(b["org"].org_id):
                assert _fetchone("SELECT count(*) FROM saloons WHERE name = 'smuggled'") == 0
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_unset_guc_sees_zero_rows(seeded_rls_db):
    # Rows DO exist (created as superuser) — the point is that the app role
    # cannot see ANY of them without context; otherwise this passes vacuously.
    _build_org("a")
    _build_org("b")
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")
    try:
        # 3. Fail-closed: no context anywhere ⇒ nothing is visible.
        for table in ("organizations", "org_members", "farms", "saloons",
                      "account_role_scopes", "flock_cycles", "command_logs"):
            assert _fetchone(f"SELECT count(*) FROM {table}") == 0, table
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_no_guc_leak_between_transactions(seeded_rls_db):
    a, b = _build_org("a"), _build_org("b")
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")
    try:
        with transaction.atomic(), tenant_context(a["org"].org_id):
            assert _fetchone("SELECT count(*) FROM saloons") == 2
        # 4. COMMIT killed the tx-local GUC (C-12: it reads back as ''), so the
        # NEXT unit of work on this SAME pooled connection starts fail-closed —
        # it can neither see A's rows nor inherit them accidentally.
        assert _fetchone("SELECT current_setting('app.current_org_id', true)") == ""
        assert _fetchone("SELECT count(*) FROM saloons") == 0

        with transaction.atomic(), tenant_context(b["org"].org_id):
            assert _fetchone("SELECT count(*) FROM saloons WHERE name LIKE 'saloon-%a'") == 0
            assert _fetchone("SELECT count(*) FROM saloons") == 2
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_login_resolution_under_enforced_rls(seeded_rls_db):
    """2026-09-11 rehearsal finding (live repro): POST /auth/login/ validated
    the password, then died with 401 "no active org membership" —
    tenant_isolation hides EVERY org_members row from a request that has no
    org claim yet, and login is exactly that request. The
    org_members_self_lookup SELECT policy + the resolver's
    app.current_user_id GUC make own-row resolution work while writes stay
    org-only and cross-org isolation stays intact."""
    province = Province.objects.create(name="prov-loginrls")
    city = City.objects.create(province=province, name="city-loginrls")
    org = Organization.objects.create(name="org-loginrls")
    user = User.objects.create_user("loginrls-owner", password="x")
    OrgMember.objects.create(org=org, user=user, role="owner")
    AccountRoleScope.objects.create(user=user, org=org, role="doctor")
    _build_org("other")  # a second org exists — must stay invisible
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")
    try:
        # The exact login/refresh claim path, as the app role, no org context.
        member, roles = resolve_active_membership(user)
        assert member is not None
        assert str(member.org_id) == str(org.org_id)
        assert roles == ["doctor", "owner"]  # sorted union, scopes included
        # The login view reads member.org.name AFTER resolve returns (GUCs
        # dead): this must hit the related-object cache from inside the
        # window, never an RLS-invisible re-query.
        assert member.org.name == "org-loginrls"
        # The cross-org membership is NOT visible via the self branch.
        assert _fetchone("SELECT count(*) FROM org_members") == 0
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
    # Deactivate as the bootstrap superuser, then prove fail-closed (C-14):
    # even the self branch must not resurrect a deactivated membership.
    OrgMember.objects.filter(user=user).update(is_active=False)
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE morghdari_app")
    try:
        with pytest.raises(AuthenticationFailed):
            resolve_active_membership(user)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_saloon_telemetry_fail_closed_manager_blocks_unscoped_queries():
    """C-11 Approach B: any SaloonTelemetry query or write on the default
    connection without an explicit org_id filter raises immediately."""
    from django.core.exceptions import PermissionDenied
    from django.db.models import Q
    from django.utils import timezone

    from apps.telemetry.models import SaloonTelemetry, UnscopedTelemetryQueryError

    with pytest.raises((PermissionDenied, RuntimeError, UnscopedTelemetryQueryError)):
        SaloonTelemetry.objects.filter(saloon_id=1)

    with pytest.raises(UnscopedTelemetryQueryError):
        SaloonTelemetry._default_manager.filter(saloon_id=1)

    with pytest.raises(UnscopedTelemetryQueryError):
        SaloonTelemetry.objects.filter(org_id=None, saloon_id=1)

    with pytest.raises(UnscopedTelemetryQueryError):
        SaloonTelemetry.objects.filter(org_id="", saloon_id=1)

    with pytest.raises(UnscopedTelemetryQueryError):
        SaloonTelemetry.objects.exclude(org_id="00000000-0000-0000-0000-000000000001")

    with pytest.raises(UnscopedTelemetryQueryError):
        SaloonTelemetry.objects.filter(
            Q(org_id="00000000-0000-0000-0000-000000000001") | Q(saloon_id=1)
        )

    with pytest.raises(UnscopedTelemetryQueryError):
        SaloonTelemetry.objects.all().count()

    with pytest.raises(UnscopedTelemetryQueryError):
        SaloonTelemetry.objects.all().exists()

    with pytest.raises(UnscopedTelemetryQueryError):
        list(SaloonTelemetry.objects.all())

    with pytest.raises(UnscopedTelemetryQueryError):
        SaloonTelemetry.objects.create(
            device_ts=timezone.now(), saloon_id=1, slot_id="S1", metrics={}
        )

    with pytest.raises(UnscopedTelemetryQueryError):
        SaloonTelemetry.objects.bulk_create(
            [SaloonTelemetry(device_ts=timezone.now(), saloon_id=1, slot_id="S1", metrics={})]
        )


def test_saloon_telemetry_compression_and_metric_hourly_rls(timescale_db):
    """C-11 Approach B against TimescaleDB: metric_hourly_aggregates keeps
    FORCE RLS; saloon_telemetry has RLS disabled, columnstore compression
    enabled, and compress_chunk succeeds without RLS errors."""
    if connection.vendor != "postgresql":
        pytest.skip("TimescaleDB compression + pg_class RLS flags require PostgreSQL")

    from datetime import datetime, timedelta, timezone as dt_tz

    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.telemetry.models import SaloonTelemetry

    # 1. Verify RLS posture on the two hypertables in pg_class.
    rls_hourly = _fetch(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'metric_hourly_aggregates'"
    )[0]
    assert rls_hourly == (True, True)

    rls_telemetry = _fetch(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'saloon_telemetry'"
    )[0]
    assert rls_telemetry == (False, False)

    # 2. Verify TimescaleDB columnstore compression is enabled on saloon_telemetry.
    compression_enabled = _fetchone(
        "SELECT compression_enabled FROM timescaledb_information.hypertables "
        "WHERE hypertable_name = 'saloon_telemetry'"
    )
    assert compression_enabled is True

    # 3. Seed a row in an old chunk and prove compress_chunk succeeds cleanly.
    a = _build_org("comp")
    saloon = a["saloons"][0]
    pos = SaloonPosition.objects.create(saloon=saloon, org=a["org"], label="center")
    slot = SaloonSensorSlot.objects.create(
        slot_id="slot-comp-1", saloon=saloon, org=a["org"], position=pos
    )
    old_ts = datetime.now(dt_tz.utc) - timedelta(days=100)
    SaloonTelemetry.objects.bulk_create(
        [
            SaloonTelemetry(
                server_received_ts=old_ts,
                device_ts=old_ts,
                org=a["org"],
                saloon=saloon,
                slot=slot,
                is_buffered=False,
                metrics={"temp": {"avg": 26.5}},
            )
        ]
    )

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT compress_chunk(i, if_not_compressed => true) "
            "FROM show_chunks('saloon_telemetry', older_than => INTERVAL '30 days') i"
        )
        compressed_chunks = cursor.fetchall()
    assert len(compressed_chunks) >= 1

    is_compressed = _fetchone(
        "SELECT bool_or(is_compressed) FROM timescaledb_information.chunks "
        "WHERE hypertable_name = 'saloon_telemetry'"
    )
    assert is_compressed is True

    # Compressed row remains readable through the scoped ORM manager.
    rows = list(
        SaloonTelemetry.objects.filter(org_id=a["org"].org_id, saloon_id=saloon.pk)
    )
    assert len(rows) == 1
    assert rows[0].metrics["temp"]["avg"] == 26.5
