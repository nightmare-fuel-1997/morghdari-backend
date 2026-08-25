"""Dead-letter writer — forensics without crashing (TUTORIAL §3.4).

Every rejected message lands in the `telemetry:dead_letter` stream carrying
topic + raw payload + REASON. Nothing is dropped silently; nothing crashes the
subscriber either — that combination is the design, not an accident.
فارسی: صندوق پیام‌های مردود — همیشه با دلیل؛ نه پیام گم می‌شود نه سرویس می‌میرد.
"""

DLQ_STREAM = "telemetry:dead_letter"


def send_to_dlq(redis_client, topic: str, raw_payload, reason: str) -> str:
    """XADD one dead-letter entry; returns the entry id."""
    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode("utf-8", errors="replace")
    return redis_client.xadd(
        DLQ_STREAM,
        {"topic": topic, "payload": raw_payload, "reason": reason},
    )
