"""Breach accumulation + hysteresis — the detection gate before any firing.

Canon (Walkthrough §4.2, TUTORIAL §3.3 lines 69–77, AU-BREACH-TTL): the breach
key holds JSON STATE — {"started_at", "alerted", "value"} — with TTL =
breach_duration_sec refreshed on EVERY consecutive breach observation
(including ones arriving after an alert already fired). The key TTL answers
"how long can this episode live unattended"; the stored started_at answers
"how long has it ACTUALLY been breaching". Tier 1 fires exactly once per
episode, when elapsed(started_at) ≥ breach_duration_sec — never before, never
twice (the original ex=900-set-once design expired the key at the very moment
the 15-minute alert should fire, so it mathematically never could).

Hysteresis (flapping prevention): once breaching, the metric stays IN BREACH
until the value drops `hysteresis_buffer_units` BELOW the max — not merely
below it. This stops a noisy sensor hovering at the line from creating
alert/resolve/alert cycles.

فارسی: تشخیصِ عبور از آستانه با تاخیرِ ۹۰۰ ثانیه و مانعِ نوسان؛ وضعیتِ نقض در
خودِ کلید ردیس (زمانِ شروع و پرچمِ هشدار) نگه‌داری می‌شود تا هشدار دقیقاً یک بار
و دقیقاً پس از ۱۵ دقیقهٔ پیوسته صادر شود.
"""

import json
import logging
import time

logger = logging.getLogger(__name__)


def _now(clock_now: float | None) -> float:
    """Injected clock wins; wall clock is the production default.

    فارسی: ساعتِ تزریقی برای تست؛ ساعتِ واقعی پیش‌فرضِ تولید.
    """
    return clock_now if clock_now is not None else time.time()


def _read_state(client, key: str) -> dict | None:
    raw = client.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # Legacy/plain-value blob (pre-M4-05 shape): treat as fresh episode.
        try:
            return {"started_at": None, "alerted": False, "value": float(raw)}
        except (TypeError, ValueError):
            return None


def track_breach(
    client,
    saloon_id: int,
    metric: str,
    value: float,
    max_threshold: float,
    hysteresis_buffer: float,
    duration_sec: int,
    clock_now: float | None = None,
) -> str:
    """Track one metric reading against its max threshold.

    Returns one of:
      "breaching" — value ≥ max OR still inside the hysteresis band
      "resolved"  — was breaching, now dropped past the hysteresis buffer
      "normal"    — never was breaching, value is fine

    The breach key TTL is REFRESHED on every consecutive breach observation
    (Walkthrough lines 763–780) while started_at PRESERVES the true episode
    age so the duration filter can fire at exactly breach_duration_sec.
    فارسی: هر مشاهدهٔ پیوسته، مهلتِ کلید را تازه می‌کند اما زمانِ شروعِ نقض
    دست‌نخورده می‌ماند.
    """
    from apps.alerts.services.redis_keys import breach_key

    key = breach_key(saloon_id, metric)
    state = _read_state(client, key)
    in_band = value >= max_threshold

    if in_band or state is not None:
        # Hysteresis: resolve ONLY when the value dropped hysteresis_buffer
        # units INSIDE (below) the threshold.
        if not in_band and value < max_threshold - hysteresis_buffer:
            client.delete(key)
            logger.info(
                "breach resolved: saloon=%s metric=%s value=%s (threshold=%s)",
                saloon_id, metric, value, max_threshold,
            )
            return "resolved"

        if state is None:
            state = {"started_at": _now(clock_now), "alerted": False}
        state["value"] = value
        client.set(key, json.dumps(state), ex=duration_sec)
        return "breaching"

    return "normal"


def consume_fire(
    client,
    saloon_id: int,
    metric: str,
    duration_sec: int,
    clock_now: float | None = None,
) -> bool:
    """True EXACTLY ONCE per breach episode, when elapsed ≥ duration_sec.

    Marks alerted=True on success (preserving the remaining TTL — the mark is
    bookkeeping, not a refresh). Idempotent: a second call returns False.
    فارسی: فقط یک بار در طولِ یک دورهٔ نقض، بعد از گذراندنِ کاملِ مهلت، True
    برمی‌گرداند.
    """
    from apps.alerts.services.redis_keys import breach_key

    key = breach_key(saloon_id, metric)
    state = _read_state(client, key)
    if not state or state.get("started_at") is None:
        return False
    if state.get("alerted"):
        return False

    elapsed = _now(clock_now) - state["started_at"]
    if elapsed < duration_sec:
        return False

    state["alerted"] = True
    remaining = client.ttl(key)
    client.set(key, json.dumps(state), ex=max(remaining, 1))
    logger.info(
        "duration met: saloon=%s metric=%s elapsed=%.0fs ≥ %ds — cleared to fire",
        saloon_id, metric, elapsed, duration_sec,
    )
    return True


def breach_value(client, saloon_id: int, metric: str) -> float | None:
    """Last value observed inside the current breach episode (None if none).

    Escalation sweep uses this to stamp current_value between firings.
    فارسی: آخرین مقدارِ ثبت‌شده در دورهٔ نقض جاری برای به‌روزرسانیِ ردیفِ هشدار.
    """
    from apps.alerts.services.redis_keys import breach_key

    state = _read_state(client, breach_key(saloon_id, metric))
    if not state or state.get("value") is None:
        return None
    try:
        return float(state["value"])
    except (TypeError, ValueError):
        return None
