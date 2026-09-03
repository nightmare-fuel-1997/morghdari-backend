"""M4-03 acceptance — NH₃ velocity bypass + temp rate lane (Decision 10).

WHY: NH₃ is lethal; max_ts ≥ 50 means a SPIKE was caught mid-window, not a
drift. Fires INSTANT Tier 1 regardless of grace keys, mutes, operational state
or any other suppression (Decision 10: evaluated FIRST). Temp rate-of-change
lane catches fast swings before they cross the static threshold. Both gated by
`{metric}_enabled`; NH₃ ALWAYS evaluates .max, never .avg.
فارسی: پذیرش M4-03 — سرعت‌سنجِ آمونیاک و دما قبل از همهٔ فیلترها اجرا می‌شود؛
آمونیاک با بیشینه سنجیده می‌شود و تغییر ناگهانی دما هم فوراً هشدار می‌دهد.
"""

import pytest
import redis as redis_lib

from apps.alerts.services.redis_keys import thresholds_hash
from apps.alerts.services.velocity import check_velocity_emergency

from tests.conftest import redis_base_url  # noqa: E402

REDIS_URL = redis_base_url() + "/10"
SALOON = 42


@pytest.fixture(autouse=True)
def redis_client():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client


def _thresholds(**overrides):
    base = {
        "temp_max": "30.00",
        "temp_min": "18.00",
        "nh3_enabled": "1",
        "operational_state": "ACTIVE_FLOCK",
    }
    base.update(overrides)
    return base


def _metrics(nh3_max_ts=None, nh3_max=None, temp_avg=None):
    metrics = {}
    if nh3_max_ts is not None:
        metrics["nh3"] = {"avg": 15.0, "max": nh3_max, "max_ts": nh3_max_ts}
    if temp_avg is not None:
        metrics["temp"] = {"avg": temp_avg, "min": 20.0, "max": temp_avg + 1.5}
    return metrics


# ── NH₃ velocity lane ──────────────────────────────────────────────────────

class TestNh3VelocityBypass:
    def test_high_max_ts_fires_instant_tier1(self, redis_client):
        metrics = _metrics(nh3_max_ts=55, nh3_max=22.0)
        result = check_velocity_emergency(
            redis_client, SALOON, _thresholds(), metrics, clock_now=1000.0,
        )
        assert result is not None
        assert result["metric"] == "nh3"
        assert result["tier"] == 1

    def test_below_spike_threshold_does_not_fire(self, redis_client):
        metrics = _metrics(nh3_max_ts=30, nh3_max=22.0)
        result = check_velocity_emergency(
            redis_client, SALOON, _thresholds(), metrics, clock_now=1000.0,
        )
        assert result is None

    def test_nh3_disabled_flag_prevents_firing_even_with_high_max_ts(self, redis_client):
        thresholds = _thresholds(nh3_enabled="0")
        metrics = _metrics(nh3_max_ts=55, nh3_max=22.0)
        result = check_velocity_emergency(
            redis_client, SALOON, thresholds, metrics, clock_now=1000.0,
        )
        assert result is None

    def test_no_nh3_key_means_no_check(self, redis_client):
        thresholds = _thresholds()
        del thresholds["nh3_enabled"]
        metrics = {"temp": {"avg": 28.0}}
        result = check_velocity_emergency(
            redis_client, SALOON, thresholds, metrics, clock_now=1000.0,
        )
        assert result is None

    def test_nh3_uses_max_not_avg(self, redis_client):
        """Invariant: NH₃ ALWAYS evaluates .max — avg can be normal while max
        caught a brief spike."""
        metrics = {
            "nh3": {
                "avg": 14.0,   # normal avg
                "max": 25.0,   # dangerous spike caught in this window
                "max_ts": 55,  # spike timestamp qualifies
            }
        }
        result = check_velocity_emergency(
            redis_client, SALOON, _thresholds(), metrics, clock_now=1000.0,
        )
        assert result is not None
        assert result["value"] == 25.0  # the MAX value, not avg


# ── temperature rate-of-change lane ────────────────────────────────────────

class TestTempRateLane:
    def test_fast_rise_fires(self, redis_client):
        last_key = f"saloon:{SALOON}:last_metrics"
        redis_client.hset(last_key, mapping={"temp_avg": "24.0", "ts": "940.0"})
        metrics = _metrics(temp_avg=29.5)  # +5.5°C delta ≥ threshold 5.0
        result = check_velocity_emergency(
            redis_client, SALOON, _thresholds(), metrics, clock_now=1000.0,
        )
        assert result is not None
        assert "rate-of-change" in result["reason"] or "rate" in result["reason"].lower()

    def test_small_change_does_not_fire(self, redis_client):
        last_key = f"saloon:{SALOON}:last_metrics"
        redis_client.hset(last_key, mapping={"temp_avg": "28.0", "ts": "940.0"})
        metrics = _metrics(temp_avg=28.5)  # +0.5°C well under threshold
        result = check_velocity_emergency(
            redis_client, SALOON, _thresholds(), metrics, clock_now=1000.0,
        )
        assert result is None
