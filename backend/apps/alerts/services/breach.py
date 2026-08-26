"""Breach accumulation + hysteresis — the detection gate before any firing.

Canon (Walkthrough §4.2, Saas D9/D14): a metric is IN BREACH when its value
exceeds `{metric}_max`. The breach key TTL doubles as the DURATION TIMER:
it must survive `breach_duration_sec` of continuous breaching before an alert
fires. TTL is REFRESHED on every consecutive breach observation (including
ones that arrive after an alert already fired) so a live breach never times out.

Hysteresis (flapping prevention): once breaching, the metric stays IN BREACH
until the value drops `hysteresis_buffer_units` BELOW the max — not merely
below it. This stops a noisy sensor hovering at the line from creating
alert/resolve/alert cycles.

فارسی: تشخیصِ عبور از آستانه با تاخیرِ ۹۰۰ ثانیه و مانعِ نوسان — مقدار باید
به‌اندازهٔ حافظهٔ هیسترزیس پایین‌تر بیاید تا هشدار بسته شود.
"""

import logging

logger = logging.getLogger(__name__)


def track_breach(
    client,
    saloon_id: int,
    metric: str,
    value: float,
    max_threshold: float,
    hysteresis_buffer: float,
    duration_sec: int,
) -> str:
    """Track one metric reading against its max threshold.

    Returns one of:
      "breaching" — value ≥ max OR still inside the hysteresis band
      "resolved"  — was breaching, now dropped past the hysteresis buffer
      "normal"    — never was breaching, value is fine

    The breach key TTL is REFRESHED on every consecutive breach observation
    (Walkthrough lines 763–780: "TTL refreshed on EVERY consecutive breach obs
    including already-alerted") so a live breach never times out mid-alarm.
    فارسی: هر بار که مقدار در محدودهٔ نقض باشد، مهلتِ کلید تازه می‌شود.
    """
    from apps.alerts.services.redis_keys import breach_key

    key = breach_key(saloon_id, metric)
    in_band = value >= max_threshold
    was_tracked = bool(client.exists(key))

    if in_band:
        # Refresh TTL on every consecutive breach observation — even ones
        # arriving after an alert already fired (Walkthrough:763–780).
        client.set(key, repr(value), ex=duration_sec)
        return "breaching"

    if was_tracked:
        # Was breaching, now below the line. Hysteresis: resolve ONLY when the
        # value dropped hysteresis_buffer_units INSIDE (below) the threshold.
        if value < max_threshold - hysteresis_buffer:
            client.delete(key)
            logger.info(
                "breach resolved: saloon=%s metric=%s value=%s (threshold=%s)",
                saloon_id, metric, value, max_threshold,
            )
            return "resolved"
        # Still inside the hysteresis band — treat as breaching, refresh TTL.
        client.set(key, repr(value), ex=duration_sec)
        return "breaching"

    return "normal"
