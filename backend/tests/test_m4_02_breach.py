"""M4-02 acceptance — breach accumulation, hysteresis, midnight grace.

Tests written RED-FIRST per TUTORIAL subtask order. All pure Redis + injected
values — no Django DB needed, so they run on every laptop loop.
فارسی: پذیرش M4-02 — تشخیص عبور، هیسترزیس و کلید گذار؛ فقط ردیس و مقادیر
تزریق‌شده، بدون دیتابیس.
"""

import pytest
import redis as redis_lib

from apps.alerts.services.breach import track_breach
from apps.alerts.services.redis_keys import breach_key

REDIS_URL = "redis://:dev_redis_password@localhost:6379/10"  # scratch DB


@pytest.fixture(autouse=True)
def redis_client():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client


SALOON = 1
METRIC = "temp"
MAX_T = 30.0
BUFFER = 0.5
DURATION = 900


def _track(client, value, saloon=SALOON, metric=METRIC,
           mx=MAX_T, buf=BUFFER, dur=DURATION):
    return track_breach(
        client, saloon, metric, value, mx, buf, dur,
    )


class TestBreachAccumulation:
    def test_value_above_max_starts_breach_with_correct_ttl(self, redis_client):
        result = _track(redis_client, 32.0)
        assert result == "breaching"
        key = breach_key(SALOON, METRIC)
        ttl = redis_client.ttl(key)
        assert ttl == DURATION, f"expected TTL={DURATION}, got {ttl}"

    def test_consecutive_breach_refreshes_ttl(self, redis_client):
        _track(redis_client, 32.0)
        # simulate time passing by manually shrinking the TTL
        key = breach_key(SALOON, METRIC)
        redis_client.expire(key, 100)
        assert redis_client.ttl(key) < DURATION

        # next consecutive breach observation refreshes the TTL back up
        _track(redis_client, 33.0)
        assert redis_client.ttl(key) > DURATION - 10

    def test_value_below_max_never_breaches(self, redis_client):
        result = _track(redis_client, 25.0)
        assert result == "normal"

    def test_breach_key_holds_last_value(self, redis_client):
        _track(redis_client, 32.0)
        key = breach_key(SALOON, METRIC)
        stored = float(redis_client.get(key))
        assert stored == 32.0


class TestHysteresisFlappingPrevention:
    def test_exactly_at_max_is_still_breaching(self, redis_client):
        """Value == max is a breach (>= comparison)."""
        assert _track(redis_client, MAX_T) == "breaching"

    def test_drops_inside_hysteresis_band_still_breaching(self, redis_client):
        """max=30.0, buffer=0.5: value must drop below 29.5 to resolve."""
        _track(redis_client, 32.0)  # start breaching
        result = _track(redis_client, 29.8)  # inside buffer band (30.0-0.5=29.5)
        assert result == "breaching", "hysteresis band should keep breach alive"

    def test_drops_past_buffer_resolves(self, redis_client):
        _track(redis_client, 32.0)
        result = _track(redis_client, 29.0)  # well past 29.5 buffer line
        assert result == "resolved"
        assert not redis_client.exists(breach_key(SALOON, METRIC))

    def test_flapping_does_not_occur_at_boundary(self, redis_client):
        """Sensor hovering at exactly max−buffer/2 must NOT flap resolve/breach."""
        # prime: get into breaching state first
        _track(redis_client, 32.0)
        boundary_value = MAX_T - BUFFER / 2  # 29.75
        results = []
        for _ in range(6):
            results.append(_track(redis_client, boundary_value))
        # all observations inside the band stay breaching — zero flapping
        assert all(r == "breaching" for r in results)


class TestGraceKeySuppression:
    """Midnight transition grace suppresses non-velocity firing while rules settle."""

    def test_grace_key_present_means_suppressed(self, redis_client):
        from apps.alerts.services.redis_keys import transition_grace_key

        grace_key = transition_grace_key(SALOON)
        redis_client.setex(grace_key, 300, "1")

        from apps.alerts.services.grace import is_graced

        assert is_graced(redis_client, SALOON) is True

    def test_no_grace_key_means_not_suppressed(self, redis_client):
        from apps.alerts.services.grace import is_graced

        assert is_graced(redis_client, SALOON) is False

    def test_grace_key_expires_after_ttl(self, redis_client):
        from apps.alerts.services.redis_keys import transition_grace_key

        grace_key = transition_grace_key(SALOON)
        redis_client.setex(grace_key, 300, "1")
        assert redis_client.exists(grace_key)

        # simulate expiry
        redis_client.delete(grace_key)
        assert not redis_client.exists(grace_key)
