"""Telemetry Celery tasks — v0.7 M7-06 rehearsal fix.

The drain tasks are DEFINED in consumers.py (they share the drain machinery
and the RLS-scoped helpers), but Celery's autodiscover only imports each
app's ``tasks`` module — without this re-export the worker registered NO
telemetry task and every beat kick died with
``KeyError: 'telemetry.process_realtime_payload'`` while the streams piled
up silently (laptop rehearsal, 2026-09-08).

Keep this module a pure re-export: the canonical implementations stay in
consumers.py next to the drain code they drive.
"""

from .consumers import process_historical_payload, process_realtime_payload  # noqa: F401
