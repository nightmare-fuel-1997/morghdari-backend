"""Midnight transition grace — D9 / seeded threshold_transition_grace_sec.

When the threshold hash changes (rules edited, flock age band crossed), a
grace key is written with TTL = threshold_transition_grace_sec. While that key
exists, non-velocity alerts are SUPPRESSED — readings still logged — because
the rules are settling and firing now would produce false positives.

Velocity emergencies ALWAYS bypass this check (Decision 10, M4-03).
فارسی: کلید گذارِ نیمه‌شب باعثِ بی‌صدا شدنِ هشدارهای غیرِ سرعتی می‌شود تا
قواعد تازه جا بیفتند؛ هشدارهای سرعتی همیشه از این عبور می‌کنند.
"""

from apps.alerts.services.redis_keys import transition_grace_key


def is_graced(redis_client, saloon_id: int) -> bool:
    """True when a midnight-transition grace key is still alive for this saloon."""
    return bool(redis_client.exists(transition_grace_key(saloon_id)))
