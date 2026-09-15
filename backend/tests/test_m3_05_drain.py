"""M3-05/M3-06 acceptance — the drain: XACK-after-commit, org grouping, lanes.

WHY: effectively-once on the WRITE side, proven on real Postgres + Redis via
the REAL subscriber pipeline. Runs on TransactionTestCase WITHOUT framework
flushing (_fixture_teardown skipped): the unmanaged hypertable's outbound FK
breaks Django's TRUNCATE list, and our data is isolated anyway — every run
creates its own UUID-tagged org subtree that simply stays behind.
فارسی: پذیرش M3-05/06 روی پشتهٔ واقعی؛ هر اجرا سازمانِ با شناسهٔ یکتا می‌سازد و
پاک‌سازیِ چارچوب که با جدول اَن‌منیج ناسازگار است کلاً کنار گذاشته شده است.
"""

import json
import os
import unittest
import uuid

import pytest
import redis as redis_lib
from django.db import connection
from django.test import TransactionTestCase

# ISOLATED Redis logical DB for this module: streams/dedup/groups from other
# runs can never leak in. Flushed per test by the autouse fixture below.
from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL  # noqa: E402

from apps.accounts.models import OrgMember  # noqa: E402
from apps.devices.models import (  # noqa: E402
    GatewaySensorMapping,
    SaloonPosition,
    SaloonSensorSlot,
)
from apps.farms.models import Farm, Saloon  # noqa: E402
from apps.geography.models import City, Province  # noqa: E402
from apps.telemetry.consumers import (  # noqa: E402
    CONSUMER_GROUP,
    HistoricalPacer,
    STREAM_HISTORICAL,
    STREAM_REALTIME,
    drain_stream,
)
from apps.telemetry.models import SaloonTelemetry  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402
from morghdari.subscriber.pipeline import handle_message  # noqa: E402

PASSWORD = "strong-pass-123"


def _client():
    return redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)


def _pending(client, stream):
    return client.xpending(stream, CONSUMER_GROUP)["pending"]


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    """Wipe the isolated logical DB before every test — zero cross-run state."""
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    # consumer must read/write THIS logical db, not whatever env says
    monkeypatch.setattr("apps.telemetry.consumers._redis_client", client)
    yield


class _NoFlush(TransactionTestCase):
    """Integration base: real commits, zero framework truncation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.db import connection as dj_conn

        if dj_conn.vendor != "postgresql":
            raise unittest.SkipTest("drain truth requires PostgreSQL + Redis (CI/dev)")

    def _fixture_teardown(self):
        pass  # rows are isolated per-run by a fresh UUID org

    def _make_world(self):
        tag = uuid.uuid4().hex[:8]
        province = Province.objects.create(name=f"p-{tag}")
        city = City.objects.create(province=province, name=f"c-{tag}")
        org = Organization.objects.create(name=f"org-{tag}")
        farm = Farm.objects.create(org=org, name=f"f-{tag}", province=province, city=city)
        saloon = Saloon.objects.create(org=org, farm=farm, name=f"s-{tag}")
        position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
        slot = SaloonSensorSlot.objects.create(
            slot_id=f"slot-{tag}", saloon=saloon, org=org, position=position
        )
        GatewaySensorMapping.objects.create(
            gateway_hardware_id=f"hw-{tag}", pack_id="P-A", slot=slot, org=org
        )
        topic = f"{org.org_id}/farms/1/gateways/GW-{tag}/telemetry"
        return {"tag": tag, "org": org, "slot": slot, "topic": topic}

    def _payload_bytes(self, hardware_id, gateway_slot, device_ts, buffered=False):
        return json.dumps({
            "gateway_hardware_id": hardware_id,
            "gateway_slot_id": gateway_slot,
            "schema_version": "2.0",
            "device_ts": device_ts,
            "is_buffered": buffered,
            "readings": [{"pack_id": "P-A", "metrics": {"temp": {"avg": 28.4}}}],
        }).encode()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
class DrainTests(_NoFlush):
    def setUp(self):
        self.redis = _client()
        # streams/groups are shared dev resources: start each test clean
        self.redis.delete(STREAM_REALTIME)
        self.redis.delete(STREAM_HISTORICAL)

    def test_ingest_drain_inserts_row_and_acks(self):
        world = self._make_world()
        db_fetch = lambda *a: world["slot"].slot_id  # noqa: E731
        assert handle_message(
            self.redis, world["topic"],
            self._payload_bytes(world["tag"], f"GW-{world['tag']}", "2026-08-25T12:00:00Z"),
            db_fetch=db_fetch,
        ) is None

        acked = drain_stream(STREAM_REALTIME, count=10)
        assert acked == 1
        row = SaloonTelemetry.objects.get(org_id=world["org"].org_id)
        assert row.slot_id == world["slot"].slot_id
        assert row.is_buffered is False
        assert row.metrics["temp"]["avg"] == 28.4
        assert _pending(self.redis, STREAM_REALTIME) == 0  # acked AFTER commit

    def test_crash_before_ack_redelivers_to_exactly_one_row(self):
        # BUG-13 contract change (QA 17.1 port): batch failures are CONTAINED
        # per-row, never propagated — the old `pytest.raises` expectation
        # described the pre-quarantine behavior where a crash escaped the
        # drain. At-least-once still holds via redelivery + quarantine.
        from unittest.mock import patch

        world = self._make_world()
        db_fetch = lambda *a: world["slot"].slot_id  # noqa: E731
        assert handle_message(
            self.redis, world["topic"],
            self._payload_bytes(world["tag"], f"GW-{world['tag']}", "2026-08-25T12:00:00Z"),
            db_fetch=db_fetch,
        ) is None

        from apps.telemetry import consumers

        with patch.object(
            consumers.SaloonTelemetry.objects, "bulk_create", side_effect=RuntimeError("crash")
        ):
            drain_stream(STREAM_REALTIME, count=10)  # contained, not raised
        assert _pending(self.redis, STREAM_REALTIME) == 1  # at-least-once held

        acked = drain_stream(STREAM_REALTIME, count=10)
        assert acked == 1
        assert SaloonTelemetry.objects.filter(org_id=world["org"].org_id).count() == 1
        assert _pending(self.redis, STREAM_REALTIME) == 0

    def test_malformed_entry_dead_letters_with_processing_error(self):
        """BUG-13 follow-up (QA 17.1 port): unparseable fields must DLQ+ack,
        never escape drain_stream into an infinite redelivery loop the
        delivery-count quarantine cannot see (it only tracks batch-commit
        failures)."""
        import time

        world = self._make_world()
        marker = f"qa-malformed-{uuid.uuid4().hex[:8]}"
        self.redis.xadd(STREAM_REALTIME, {
            "org_id": str(world["org"].org_id), "farm_id": str(world["slot"].saloon.farm_id),
            "gateway_slot_id": f"GW-{world['tag']}",
            "device_ts": "2026-08-25T12:00:00Z", "is_buffered": "False",
            "slot_id": world["slot"].slot_id,
            "payload": json.dumps({"qa": marker}), "recv_ts": "not-a-float",
            "trace_id": marker, "metrics": '{"temp": 20.0}', "clock_drift": "False",
        })
        assert drain_stream(STREAM_REALTIME, count=10) == 1  # returns, does not raise
        assert _pending(self.redis, STREAM_REALTIME) == 0  # acked via normal path
        time.sleep(0.2)
        bodies = [f for _, f in self.redis.xrange("telemetry:dead_letter", min="-", max="+")]
        assert any(b.get("reason") == "PROCESSING_ERROR" and marker in (b.get("payload") or "")
                   for b in bodies)

    def test_persistent_db_failure_quarantined_after_threshold(self):
        """BUG-13 (QA 17.1): a well-formed but un-storable entry (NUMERIC
        overflow) must DLQ with PROCESSING_FAILED + XACK once deliveries
        cross realtime_dlq_after_deliveries (default 5) — never loop forever."""
        world = self._make_world()
        marker = f"qa-poison-{uuid.uuid4().hex[:8]}"
        self.redis.xadd(STREAM_REALTIME, {
            "org_id": str(world["org"].org_id), "farm_id": str(world["slot"].saloon.farm_id),
            "gateway_slot_id": f"GW-{world['tag']}",
            "device_ts": "2026-08-25T12:00:00Z", "is_buffered": "False",
            "slot_id": world["slot"].slot_id,
            "payload": json.dumps({"qa": marker}), "recv_ts": "9999999999.0",
            "trace_id": marker, "metrics": '{"temp": 20.0}', "clock_drift": "False",
        })
        for _ in range(7):  # threshold 5 crossed with margin
            drain_stream(STREAM_REALTIME, count=10)
        assert _pending(self.redis, STREAM_REALTIME) == 0  # XACK'd by quarantine
        bodies = [f for _, f in self.redis.xrange("telemetry:dead_letter", min="-", max="+")]
        assert any(b.get("reason") == "PROCESSING_FAILED" and marker in (b.get("payload") or "")
                   for b in bodies)
        assert not SaloonTelemetry.objects.filter(org_id=world["org"].org_id).exists()


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
class LaneSeparationTests(_NoFlush):
    def setUp(self):
        self.redis = _client()
        self.redis.delete(STREAM_REALTIME)
        self.redis.delete(STREAM_HISTORICAL)

    def test_buffered_payload_only_rides_historical(self):
        world = self._make_world()
        body = self._payload_bytes(
            f"hw-{world['tag']}", f"GW-{world['tag']}", "2026-08-25T09:00:00Z",
            buffered=True,
        )
        assert handle_message(self.redis, world["topic"], body, db_fetch=lambda *a: world["slot"].slot_id) is None

        assert drain_stream(STREAM_REALTIME, count=10) == 0
        assert not SaloonTelemetry.objects.filter(org_id=world["org"].org_id, is_buffered=False).exists()

        assert drain_stream(STREAM_HISTORICAL, count=10) == 1
        row = SaloonTelemetry.objects.get(org_id=world["org"].org_id)
        assert row.is_buffered is True


# ── M3-06 pacing math — pure, runs anywhere ───────────────────────────────

def test_historical_global_sleep_derives_from_config():
    pacer = HistoricalPacer(
        lambda key, default: 50 if key == "replay_rate_limit_per_min" else default,
        clock=lambda: 0.0,
    )
    assert abs(pacer.sleep_seconds("any-org") - 60.0 / 50) < 1e-9


def test_per_org_window_throttles_after_configured_rate():
    now = {"t": 100.0}
    sleeps: list[float] = []

    def fake_clock():
        now["t"] += 0.1
        return now["t"]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now["t"] += seconds  # simulated waiting advances the fake clock

    pacer = HistoricalPacer(
        lambda key, default: 20, clock=fake_clock, sleeper=fake_sleep
    )
    for _ in range(25):  # 25 rapid deliveries against a 20/min per-org cap
        pacer.wait_for_org("fixed-org")

    assert sleeps  # the throttle engaged beyond the configured rate
    assert sum(sleeps) > 0



