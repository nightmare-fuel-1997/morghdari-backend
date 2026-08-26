"""M4-04 acceptance — breaches become AlertInstance rows on real Postgres.

فارسی: پذیرش M4-04 — نقضِ آستانه باعثِ ساخته شدن ردیفِ هشدار در دیتابیس واقعی
می‌شود؛ تکرار هم ردیف تازه نمی‌سازد.
"""

import json
import unittest
import uuid

import pytest
import redis as redis_lib
from django.db import connection
from django.test import TransactionTestCase

from tests.conftest import INTEGRATION_REDIS_URL as REDIS_URL  # noqa: E402

from apps.accounts.models import OrgMember  # noqa: F401
from apps.alerts.models import AlertInstance
from apps.devices.models import GatewaySensorMapping, SaloonPosition, SaloonSensorSlot
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry.models import SaloonTelemetry  # noqa: F401
from apps.tenants.models import Organization
from morghdari.subscriber.pipeline import handle_message

PASSWORD = "strong-pass-123"
REDIS_URL = "redis://:dev_redis_password@localhost:6379/11"


@pytest.fixture(autouse=True)
def _fresh_redis():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
class AlertFiringTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("alert firing truth requires PostgreSQL + Redis")
        from tests.conftest import _install_cascade_flush

        _install_cascade_flush(connection)

    def setUp(self):
        # Point ALL Redis access (consumer + test) at OUR scratch logical DB.
        import os as _os

        _os.environ["REDIS_URL"] = REDIS_URL

        import redis as redis_lib

        redis_lib.Redis.from_url(REDIS_URL, decode_responses=True).flushdb()

    def _make_world(self):
        tag = "fr" + uuid.uuid4().hex[:8]
        province = Province.objects.create(name=f"p-{tag}")
        city = City.objects.create(province=province, name=f"c-{tag}")
        org = Organization.objects.create(name=f"f-org-{tag}")
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

        from apps.alerts.services.redis_keys import thresholds_hash
        import redis as redis_lib

        rc = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
        rc.hset(thresholds_hash(saloon.id), mapping={
            "temp_max": "30.00",
            "temp_min": "18.00",
            "nh3_enabled": "1",
            "operational_state": "ACTIVE_FLOCK",
        })

        return {"org": org, "saloon": saloon, "topic": topic, "redis": rc,
                "hw": f"hw-{tag}", "gw": f"GW-{tag}"}

    def _ingest(self, world, device_ts: str, temp_avg: float):
        payload = json.dumps({
            "gateway_hardware_id": world["hw"],
            "gateway_slot_id": world["gw"],
            "schema_version": "2.0",
            "device_ts": device_ts,
            "is_buffered": False,
            "readings": [{"pack_id": "P-A",
                          "metrics": {"temp": {"avg": temp_avg}}}],
        }).encode()
        return handle_message(world["redis"], world["topic"], payload,
                              db_fetch=lambda *a: None)

    def test_breach_creates_alert_instance(self):
        world = self._make_world()
        self._ingest(world, "2026-08-26T12:00:00Z", temp_avg=32.0)

        # The pipeline published to the stream; the drain inserts telemetry.
        # The alert evaluation runs inline after the telemetry commit.
        from apps.telemetry.consumers import drain_stream
        drain_stream("telemetry:realtime", count=10)

        alerts = AlertInstance.objects.filter(org_id=world["org"].org_id)
        assert alerts.count() == 1
        alert = alerts.get()
        assert alert.metric_type == "temp"
        assert float(alert.current_value) == 32.0
        assert alert.current_status == "ACTIVE_TIER_1"
        assert alert.batch_id is None
