"""v0.7 M7-08 — MET-SAAS per-tenant cost metering (Runbook §5.4, §4.2).

فارسی: آزمون‌های اندازه‌گیریِ مصرفِ هر سازمان (MET-SAAS) و قواعدِ
سوزاندنِ بودجهٔ دسترس‌پذیری (MET-AVAIL).

Covers:
* the Saas.md §15 meter bump (org:{org_id}:meter:current_hour, TTL 3600)
  wired into the drain (consumers.py → bump_meter_hour) and its task form
  monitoring.record_org_message,
* the doc's nightly `aggregates.build_org_usage` rollup (data-aggregates
  contract: msg_count/stored_bytes/saloon_count/gateway_count,
  UNIQUE(org_id, day) upsert) + the §5.4 tenant_cost_estimate_usd export
  through the Redis bridge into the /metrics/ export,
* the MET-AVAIL burn-rate rules exist in monitoring/alerts.yml evaluating
  probe_success (the prober contract — never self-scraped metrics), and
  prometheus.yml carries the ≥2-region 60s prober jobs.

Redis-dependent parts use the real metrics Redis (same pattern as
test_m7_04_metrics.py — the rehearsal stack has Redis; CI runs the suite
inside the compose network).
"""

from datetime import date
from pathlib import Path

import pytest
import yaml
from django.db import connection
from django.urls import reverse

from apps.monitoring.metrics import (
    METER_KEY,
    TENANT_COST_ESTIMATE_USD,
    _metrics_redis,
    bump_meter_hour,
    refresh_gauges,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_metric_keys():
    client = _metrics_redis()
    try:
        for key in client.scan_iter("metrics:*"):
            client.delete(key)
        for key in client.scan_iter("org:*:meter:*"):
            client.delete(key)
    except Exception:  # noqa: BLE001 — Redis down: tests fail loudly anyway
        pass
    yield


def _meter_get(org_id):
    return _metrics_redis().get(METER_KEY.format(org_id=org_id))


# ---------------------------------------------------------------------------
# §15 meter bump — the counter, its TTL, and its producer-side silence rule
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=False)
def test_bump_meter_hour_incrs_with_ttl():
    r = _metrics_redis()
    bump_meter_hour(r, "org-1", 3)
    bump_meter_hour(r, "org-1", 2)
    assert int(_meter_get("org-1")) == 5
    ttl = r.ttl(METER_KEY.format(org_id="org-1"))
    assert 0 < ttl <= 3600  # Saas.md §15: TTL = 3600s


def test_meter_bump_is_silent_on_redis_failure():
    # Producer-side bumps must NEVER break the hot path (same contract as
    # every other bump in the bridge).

    class _Boom:
        def incrby(self, *a, **k):
            raise ConnectionError("redis down")

        def expire(self, *a, **k):
            raise ConnectionError("redis down")

    bump_meter_hour(_Boom(), "org-1", 1)  # must not raise


# ---------------------------------------------------------------------------
# MET-SAAS rollup — build_org_usage (admin-alias cross-org sweep)
# ---------------------------------------------------------------------------


@pytest.fixture
def _usage_stack():
    """Table provisioning for the unmanaged rollup tables.

    * org_daily_usage is unmanaged AND its migration is PG-guarded, so under
      --nomigrations (pytest.ini) it exists NOWHERE by default — this fixture
      creates a portable DDL on BOTH vendors, vendor-branched for the id
      column. Deliberately WITHOUT the RLS policy: policy truth is proven by
      test_rls.py + scripts/check_rls_parity.py; this suite tests the rollup
      LOGIC. Runs inside the test's own atomic block (transaction=False), so
      the CREATE rolls back with the test — per-test isolation for free.
    * saloon_telemetry: on sqlite this fixture replays the same portable DDL
      the M7-12 fixture uses (incl. the generated buffered_delay_mins); on
      PostgreSQL the timescale_db session fixture replays the real
      hypertable DDL (M3-09/M6-04 convention) — via the direct session
      fixture below, since these tests use transaction=False.
    """
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS org_daily_usage ("
                "id BIGSERIAL PRIMARY KEY, "
                "org_id UUID NOT NULL REFERENCES organizations(org_id), "
                "day DATE NOT NULL, msg_count BIGINT NOT NULL DEFAULT 0, "
                "stored_bytes BIGINT NOT NULL DEFAULT 0, "
                "saloon_count INTEGER NOT NULL DEFAULT 0, "
                "gateway_count INTEGER NOT NULL DEFAULT 0, "
                "CONSTRAINT org_daily_usage_org_day UNIQUE (org_id, day))"
            )
        else:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS org_daily_usage ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "org_id CHAR(32) NOT NULL REFERENCES organizations(org_id), "
                "day DATE NOT NULL, msg_count BIGINT NOT NULL DEFAULT 0, "
                "stored_bytes BIGINT NOT NULL DEFAULT 0, "
                "saloon_count INTEGER NOT NULL DEFAULT 0, "
                "gateway_count INTEGER NOT NULL DEFAULT 0, "
                "CONSTRAINT org_daily_usage_org_day UNIQUE (org_id, day))"
            )
        if connection.vendor == "sqlite":
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS saloon_telemetry ("
                "server_received_ts TIMESTAMP NOT NULL PRIMARY KEY, device_ts TIMESTAMP NOT NULL, "
                "org_id VARCHAR(32) NOT NULL, saloon_id INTEGER NOT NULL, slot_id VARCHAR(50) NOT NULL, "
                "is_buffered BOOLEAN, clock_drift BOOLEAN, metrics TEXT NOT NULL, "
                "buffered_delay_mins NUMERIC GENERATED ALWAYS AS ("
                "(julianday(server_received_ts) - julianday(device_ts)) * 1440.0) STORED)"
            )
            # Per-test isolation: sqlite teardown never truncates unmanaged
            # tables and server_received_ts is the PK — stale rows collide.
            cursor.execute("DELETE FROM saloon_telemetry")
    yield


@pytest.mark.django_db(transaction=False)
def test_build_org_usage_upserts_and_exports_cost(_usage_stack, monkeypatch):
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Farm, Saloon
    from apps.geography.models import City, Province
    from apps.monitoring.models import OrgDailyUsage
    from apps.monitoring.tasks import build_org_usage
    from apps.telemetry.models import SaloonTelemetry
    from apps.tenants.models import Organization

    day = date(2026, 9, 8)

    # Day-window counting is a plain ORM query chain — real rows beat mocking
    # the ORM. Seed through the REAL topology chain (FKs are RESTRICT): the
    # two readings sit in DIFFERENT saloons via DIFFERENT slots so the
    # distinct-count assertions mean something.
    province = Province.objects.create(name="meter-prov")
    city = City.objects.create(province=province, name="meter-city")
    org = Organization.objects.create(name="Meter Org", subscription_tier="basic")
    farm = Farm.objects.create(org=org, name="meter-farm", province=province, city=city)
    saloon_a = Saloon.objects.create(org=org, farm=farm, name="meter-sa")
    saloon_b = Saloon.objects.create(org=org, farm=farm, name="meter-sb")
    position = SaloonPosition.objects.create(saloon=saloon_a, org=org, label="north")
    slot_a = SaloonSensorSlot.objects.create(
        slot_id="meter-slot-a", saloon=saloon_a, org=org, position=position
    )
    slot_b = SaloonSensorSlot.objects.create(
        slot_id="meter-slot-b", saloon=saloon_b, org=org, position=position
    )
    SaloonTelemetry.objects.create(
        org=org,
        saloon=saloon_a,
        slot=slot_a,
        device_ts="2026-09-08T06:00:00+00:00",
        server_received_ts="2026-09-08T06:00:10+00:00",
        metrics={"temp": {"avg": 30.0}},
        is_buffered=False,
    )
    SaloonTelemetry.objects.create(
        org=org,
        saloon=saloon_b,
        slot=slot_b,
        device_ts="2026-09-08T07:00:00+00:00",
        server_received_ts="2026-09-08T07:00:10+00:00",
        metrics={"nh3": {"avg": 12.0}},
        is_buffered=False,
    )

    upserts = build_org_usage(day=day)
    assert upserts >= 1

    row = OrgDailyUsage.objects.get(org_id=org.org_id, day=day)
    assert row.msg_count == 2
    assert row.saloon_count == 2
    assert row.gateway_count == 2
    assert row.stored_bytes > 0  # sqlite leg: serialized metrics bytes

    # Cost export through the bridge → gauge (sole org ⇒ 100% ⇒ full fleet).
    raw = _metrics_redis().get(f"metrics:tenant_cost:{org.org_id}")
    assert raw is not None
    assert float(raw) == pytest.approx(40.0)
    refresh_gauges()
    assert (
        TENANT_COST_ESTIMATE_USD.labels(org_id=str(org.org_id))._value.get()
        == pytest.approx(40.0)
    )

    # Idempotent upsert on rerun (doc: "org_daily_usage (upsert)").
    upserts2 = build_org_usage(day=day)
    assert upserts2 >= 1
    assert OrgDailyUsage.objects.filter(org_id=org.org_id, day=day).count() == 1


@pytest.mark.django_db(transaction=False)
def test_build_org_usage_empty_day_exports_zero(_usage_stack):
    # A zero-traffic org still gets its ledger row with an explicit 0.0 cost
    # share — absent ≠ free (the §5.4 fixed-cost split must charge idle orgs).
    from apps.monitoring.models import OrgDailyUsage
    from apps.monitoring.tasks import build_org_usage
    from apps.tenants.models import Organization

    org = Organization.objects.create(name="Idle Org", subscription_tier="basic")
    day = date(2026, 9, 8)

    build_org_usage(day=day)
    row = OrgDailyUsage.objects.get(org_id=org.org_id, day=day)
    assert row.msg_count == 0
    assert row.stored_bytes == 0
    assert float(_metrics_redis().get(f"metrics:tenant_cost:{org.org_id}")) == 0.0


def test_record_org_message_task_bumps_meter():
    from apps.monitoring.tasks import record_org_message

    record_org_message("org-task", 4)
    assert int(_meter_get("org-task")) == 4


# ---------------------------------------------------------------------------
# /metrics/ export — the gauge reaches daphne's scrape
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=False)
def test_tenant_cost_gauge_appears_in_metrics_export():
    from django.test import Client

    resp = Client().get(reverse("monitoring:metrics"))
    assert resp.status_code == 200
    assert "tenant_cost_estimate_usd" in resp.content.decode()


# ---------------------------------------------------------------------------
# MET-AVAIL — the burn-rate rules + prober contract (config-file assertions)
# ---------------------------------------------------------------------------


def _load_rel(rel):
    with open(REPO_ROOT / rel, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_burn_rate_rules_match_runbook_42():
    rules = _load_rel("monitoring/alerts.yml")
    groups = {g["name"]: g for g in rules["groups"]}
    burn = groups["morghdari-availability-burn"]
    alerts = {r["alert"]: r for r in burn["rules"]}

    fast = alerts["AvailabilityBudgetFastBurn"]
    assert fast["expr"] == "avg_over_time(probe_success[1h]) < 0.99"  # §4.2: >1%/1h
    assert fast["labels"]["severity"] == "critical"  # 🔴 PAGE
    slow = alerts["AvailabilitySlowBurn"]
    assert slow["expr"] == "avg_over_time(probe_success[6h]) < 0.998"  # §4.2: >0.2%/6h
    assert slow["labels"]["severity"] == "warning"  # 🟠 WARNING
    # The prober contract: evaluate on probe_success (prober data), NOT on
    # self-scraped app metrics (django_http_* / telemetry_* series).
    for rule in (fast, slow):
        assert "probe_success" in rule["expr"]


def test_prober_contract_two_regions_60s():
    prom = _load_rel("monitoring/prometheus.yml")
    jobs = {j["job_name"]: j for j in prom["scrape_configs"]}
    jobs = {k: v for k, v in jobs.items() if k.startswith("blackbox-external")}
    assert len(jobs) == 2  # ≥ 2 regions (§4.2 note)
    targets = []
    modules = set()
    for job in jobs.values():
        assert job["scrape_interval"] == "60s"  # 60 s cadence (§4.2 note)
        modules.add(job["params"]["module"][0])
        targets += job["static_configs"][0]["targets"]
    # The public leg pins the cert's SAN (real SNI handshake); the internal
    # leg rides the container network. Modules may differ per leg (TLS vs
    # plain) — but the probed ROUTE must be the doc's health endpoint.
    assert any(t.endswith("/api/v1/health/") for t in targets)
    assert any(t.startswith("https://") for t in targets)  # public vantage
    assert any(t.startswith("http://") for t in targets)   # app-direct vantage
    assert modules <= {"http_2xx", "http_2xx_tls"}
