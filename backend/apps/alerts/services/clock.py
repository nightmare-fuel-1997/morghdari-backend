"""Injectable clock — every time-based decision goes through this (M4-02+).

WHY: escalation windows, breach TTLs and flock-age days are all time math.
Tests inject a fake clock (HistoricalPacer precedent, consumers.py) instead of
sleeping or freezing globals — the same pattern three times is a convention.

فارسی: ساعتِ تزریقی برای همهٔ تصمیم‌های زمانی تا تست‌ها بدون خواب واقعی باشند.
"""

import time


class Clock:
    def __init__(self, now_fn=time.time, sleep_fn=time.sleep):
        self._now = now_fn
        self._sleep = sleep_fn

    def now(self) -> float:
        return self._now()

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)


def system_clock() -> Clock:
    return Clock()
