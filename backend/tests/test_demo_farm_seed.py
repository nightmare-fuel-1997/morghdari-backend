"""demo_farm_seed management command — idempotency, thresholds, pacing,
multi-farm topology and legacy convergence.

فارسی: تستِ دستور بذرِ مزرعه‌ی نمایشی — چندباراجرا-امن بودن، کشِ آستانه، نرخِ دمو
و جذبِ سالن‌های قدیمیِ تک‌مزرعه‌ای.
"""

import json

import pytest
import redis as redis_lib
from django.core.management import call_command

from tests.conftest import redis_base_url  # noqa: E402

REDIS_URL = redis_base_url() + "/11"

from apps.alerts.services import redis_keys as keys  # noqa: E402
from apps.alerts.services.thresholds import _redis as _thr_redis  # noqa: E402
from apps.farms.models import Farm, Saloon  # noqa: E402
from apps.geography.models import City, Province  # noqa: E402
from apps.system_config.models import SystemConfiguration  # noqa: E402
from apps.tenants.models import Organization  # noqa: E402

FARM_NAMES = {"Baharestan", "Morvarid", "Nahavand"}


@pytest.fixture(autouse=True)
def _fresh_redis(monkeypatch):
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr("apps.alerts.services.thresholds._redis_client", client)
    yield


@pytest.fixture(autouse=True)
def _restore_pacing_after():
    """The seed command mutates the shared system_configuration catalog; put
    the seeded 900 s back so later modules read real seeded truth (C-16)."""
    yield
    SystemConfiguration.objects.filter(key="breach_duration_sec").update(value="900")


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_seed_is_idempotent_and_creates_thresholds(timescale_db):
    call_command("demo_farm_seed")
    orgs = Organization.objects.filter(name="Demo Farm")
    assert orgs.count() == 1
    org = orgs.get()
    assert Saloon.objects.filter(org=org).count() == 6
    assert Farm.objects.filter(org=org).count() == 3
    assert set(Farm.objects.filter(org=org).values_list("name", flat=True)) == FARM_NAMES
    for farm in Farm.objects.filter(org=org):
        assert Saloon.objects.filter(org=org, farm=farm).count() == 2

    # second run: same counts, no duplication
    call_command("demo_farm_seed")
    assert Organization.objects.filter(name="Demo Farm").count() == 1
    assert Saloon.objects.filter(org=org).count() == 6
    assert Farm.objects.filter(org=org).count() == 3

    # demo pacing is applied
    assert (
        SystemConfiguration.objects.get(key="breach_duration_sec").value == "30"
    )

    # every saloon has a cached threshold hash with nh3_enabled + temp_max
    client = _thr_redis()
    for saloon in Saloon.objects.filter(org=org):
        h = client.hgetall(keys.thresholds_hash(saloon.pk))
        assert h, f"threshold hash missing for saloon {saloon.pk}"
        assert h["nh3_enabled"] == "1"
        assert "temp_max" in h
        assert "temp_min" in h


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_seed_restore_reverts_pacing(timescale_db):
    call_command("demo_farm_seed")
    call_command("demo_farm_seed", "--restore")
    assert (
        SystemConfiguration.objects.get(key="breach_duration_sec").value == "900"
    )


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_seed_writes_manifest(timescale_db, tmp_path):
    manifest_path = tmp_path / "farm.json"
    call_command("demo_farm_seed", "--manifest-out", str(manifest_path))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["org_name"] == "Demo Farm"
    assert len(manifest["farms"]) == 3
    assert len(manifest["saloons"]) == 6
    for farm in manifest["farms"]:
        assert farm["name"] in FARM_NAMES
        assert len(farm["saloons"]) == 2
    for saloon in manifest["saloons"]:
        assert saloon["farm_id"] is not None
        assert saloon["farm_name"] in FARM_NAMES
        assert len(saloon["slots"]) == 2
        for slot in saloon["slots"]:
            assert slot["pack"] in ("P-A", "P-B")
            assert slot["slot_id"]
            assert slot["hardware_id"]
    assert manifest["breach_duration_sec"] == "30"


@pytest.mark.use_migrations
@pytest.mark.django_db(transaction=True)
def test_seed_absorbs_legacy_single_farm_topology(timescale_db):
    """A DB seeded by the OLD single-farm version converges: the legacy saloon
    is renamed + reassigned into the new topology, never duplicated or deleted."""
    org = Organization.objects.create(name="Demo Farm", subscription_tier="demo")
    province = Province.objects.create(name="Demo Province")
    city = City.objects.create(province=province, name="Demo City")
    old_farm = Farm.objects.create(org=org, name="Demo Farm", province=province, city=city)
    legacy = Saloon.objects.create(org=org, farm=old_farm, name="Demo Saloon 01")

    call_command("demo_farm_seed")

    assert Saloon.objects.filter(org=org).count() == 6
    # the three named farms exist; the legacy farm is kept but empty
    assert Farm.objects.filter(org=org, name__in=FARM_NAMES).count() == 3
    absorbed = Saloon.objects.get(pk=legacy.pk)
    assert absorbed.name == "Saloon A"
    assert absorbed.farm.name == "Baharestan"
    # the emptied legacy farm stays (RESTRICT FKs forbid deletion) but has no saloons
    assert Saloon.objects.filter(org=org, farm=old_farm).count() == 0
