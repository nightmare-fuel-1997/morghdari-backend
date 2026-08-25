"""morghdari — out-of-request worker processes (Blueprint §4/§5).

Django apps handle HTTP; THIS package hosts long-running processes that live
OUTSIDE the request cycle: the MQTT telemetry subscriber (M3-01) and, later,
any daemon-side helpers. They reuse Django settings only where explicitly
imported — keeping import cost near zero so processes boot fast.

فارسی: فرایندهای خارج از چرخهٔ درخواست — مشترک MQTT و سرویس‌های بلندمدت دیگر.
"""
