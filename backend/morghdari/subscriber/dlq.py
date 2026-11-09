"""Dead-letter writer — forensics without crashing (TUTORIAL §3.4).

Every rejected message lands in the `telemetry:dead_letter` stream carrying
topic + raw payload + REASON. Nothing is dropped silently; nothing crashes the
subscriber either — that combination is the design, not an accident.
فارسی: صندوق پیام‌های مردود — همیشه با دلیل؛ نه پیام گم می‌شود نه سرویس می‌میرد.
"""

import redis

DLQ_STREAM = "telemetry:dead_letter"


class DLQWriteError(redis.exceptions.RedisError):
    """Raised when writing to the dead-letter queue fails."""


def send_to_dlq(redis_client, topic: str, raw_payload, reason: str) -> str:
    """XADD one dead-letter entry; returns the entry id."""
    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode("utf-8", errors="replace")
    try:
        return redis_client.xadd(
            DLQ_STREAM,
            {"topic": topic, "payload": raw_payload, "reason": reason},
        )
    except (redis.exceptions.RedisError, OSError) as exc:
        raise DLQWriteError(f"Failed to persist rejection to DLQ ({reason}): {exc}") from exc

